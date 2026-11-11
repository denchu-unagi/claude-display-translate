import json
import os
from pathlib import Path
import re
import runpy
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "claude-display-translate.py"
MARK = "[自動翻訳]\n"
FAIL_MARK = "[翻訳失敗: 原文を表示]\n"
MOSTLY_JA_MARK = "[日本語が大半のため原文を表示]\n"
SOURCE = "Approved. Committing, pushing, and updating the PR body."
ENGLISH = [
    "The build failed because `npm ci` could not resolve a peer dependency.\n",
    "Run `bun test` and read https://example.com/docs for details.\n",
]
# 英語の1行目に続く、ほぼ日本語の本文。見出し・リストの日本語の行と、インデントした英語の行を含む。
# 予約タグを含む日本語の行も、伏せた本文のまま戻ることを確かめる。
MOSTLY_JAPANESE = (
    "## 状態\n\n"
    "1. コミットをプッシュしました。レビューもおわりました。\n"
    "2. あとはマージをまつだけです。<source> タグはそのままのこします。\n\n"
    "    - Parse the config and validate it before saving.\n"
)
WAIT_SECONDS = 10


def payload(message_id, index, final, delta, session_id="session1"):
    return json.dumps({"hook_event_name": "MessageDisplay", "session_id": session_id, "turn_id": "turn-1",
                       "message_id": message_id, "index": index, "final": final, "delta": delta})


# 出力なしは、Claude Code が元のdeltaを表示することを表す。
def displayed(stdout):
    return None if stdout == "" else json.loads(stdout)["hookSpecificOutput"]["displayContent"]


class HookTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="claude-msgdisp-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("MSGDISP_TRANSLATE_CHILD", "MSGDISP_CHILD_TIMEOUT")}
        self.env.update(MSGDISP_FORCE_ATTENDED="1", MSGDISP_STATE_DIR=str(self.state))
        # 開発者の手元の翻訳の設定（hooks/claude-display-translate/settings.py）を既存テストが拾わないようにする。
        self.env["MSGDISP_CONFIG_DIR"] = str(self.root / "no-config")

    def use_child(self, source):
        child = self.root / "claude.py"
        child.write_text(source, encoding="utf-8")
        self.env["MSGDISP_CLAUDE_CMD"] = json.dumps([sys.executable, str(child)])

    # 実CLIの代わりに、各行へカナを足して返す偽翻訳を使う。hookの忠実性検査を通る形に保つ。
    # 原文はsystem promptファイルから読み、ユーザーメッセージ（stdin）へ混ざらないことも確かめる。
    def use_translator(self, exit_code=0, name="translator"):
        marker = self.root / (name + "-called")
        translator = self.root / (name + ".py")
        translator.write_text("\n".join([
            "import re, sys",
            "open(%r, 'w').close()" % str(marker),
            "if %d: sys.exit(%d)" % (exit_code, exit_code),
            "assert '<source>' not in sys.stdin.read()",
            "prompt = open(sys.argv[sys.argv.index('--system-prompt-file') + 1], encoding='utf-8').read()",
            "source = re.search(r'<source>\\n(.*)\\n</source>', prompt, re.S).group(1)",
            "lines = [line + ' やく' if line else line for line in source.split('\\n')]",
            "print('<translation>\\n' + '\\n'.join(lines) + '\\n</translation>')",
            "",
        ]), encoding="utf-8")
        self.env["MSGDISP_CLAUDE_CMD"] = json.dumps([sys.executable, str(translator)])
        return marker

    def flush(self, index, final, delta, message_id="message-1", session_id="session1"):
        result = subprocess.run([sys.executable, "-B", str(HOOK)], input=payload(message_id, index, final, delta, session_id),
                                capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return displayed(result.stdout)

    def run_flushes(self, deltas, message_id="message-1"):
        return [self.flush(i, i == len(deltas) - 1, delta, message_id) for i, delta in enumerate(deltas)]

    def final_once(self, message_id):
        return self.flush(0, True, SOURCE, message_id, "standalone")

    def runtime(self):
        # run_childの時間予算はimport時刻から数えるので、テストごとに読み込み直す。
        return runpy.run_path(str(HOOK), run_name="hook_check")

    def log(self):
        return (self.state / "translate.log").read_text(encoding="utf-8")


class ChildInvocationTest(HookTestCase):
    def test_hook_is_executable(self):
        self.assertTrue(HOOK.is_file())
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_translates_with_fixed_child_invocation(self):
        self.env["CLAUDECODE"] = "1"
        self.use_child("\n".join([
            "import os, sys",
            "assert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'",
            "assert 'CLAUDECODE' not in os.environ",
            "assert sys.argv[sys.argv.index('--model') + 1] == 'claude-haiku-5-5'",
            "assert sys.argv[sys.argv.index('--effort') + 1] == 'low'",
            "print('<translation>承認済みです。コミット、push、PR本文の更新を行います。</translation>')",
            "",
        ]))
        self.assertIn("承認済み", self.final_once("success"))

    def test_a_translation_with_a_word_in_another_script_is_rejected(self):
        # 語がキリル文字などに化けた訳文は、仮名を含み構造も原文と同じなので、ほかの検査では止まらない。
        for word, script in (("локаль", "cyrillic"), ("বাংলা", "bengali")):
            with self.subTest(script=script):
                self.use_child("print(%r)" % ("<translation>" + word + "承認済みです。コミット、push、PR本文の更新を行います。</translation>"))
                shown = self.final_once("foreign-" + script)
                self.assertTrue(shown.startswith(FAIL_MARK), shown)
                self.assertIn("result=foreign_script", self.log())
                self.assertIn("script=" + script, self.log())

    def test_other_scripts_in_the_source_or_single_symbols_are_kept(self):
        # 原文にある別の文字体系の語と、「β版」のような1文字の記号は、訳文に残ってよい。
        self.use_translator()
        shown = self.flush(0, True, "The UI label reads привет for new users of the beta.", "cyrillic", "standalone")
        self.assertIn("привет", shown)
        self.assertIn("やく", shown)
        self.use_child("print(%r)" % "<translation>β版の承認済みです。コミット、push、PR本文の更新を行います。</translation>")
        self.assertIn("β版", self.final_once("greek"))

    def test_output_that_translates_child_context_is_rejected(self):
        # 子CLIが足した文脈を訳した出力は、仮名と原文の断片があっても訳文として表示しない。
        echoes = ("<system-reminder>\nメールアドレスを参照できます。\n</system-reminder>\n" + SOURCE,
                  "<source>\n承認済みです。\n</source>",
                  "メールを参照できます。</translation>\n" + SOURCE,
                  "作業ディレクトリと日付の情報です。" * 20)
        for number, echoed in enumerate(echoes):
            with self.subTest(echoed=echoed[:20]):
                self.use_child("print(%r)" % ("<translation>\n" + echoed + "\n</translation>"))
                self.assertIn("翻訳失敗", self.final_once("echo-%d" % number))
        self.assertEqual(self.log().count("echoed_context"), len(echoes))

    def test_reply_without_kana_is_logged_on_one_line(self):
        # 仮名の無い返答は原因を確かめられるよう先頭100文字だけ記録し、改行を含んでも1行のkey=valueに保つ。
        head = "Still English.\n" + "x" * 85
        self.use_child("print(%r)" % ("<translation>\n" + head + "y" * 20 + "\n</translation>"))
        self.assertIn("翻訳失敗", self.final_once("no-kana"))
        [line] = self.log().splitlines()
        self.assertIn(" result=no_japanese message=standalo-no-kana elapsed_sec=", line)
        self.assertTrue(line.endswith(" reply_head=" + json.dumps(head)), line)

    def test_echoed_source_is_rejected_even_with_japanese_quote(self):
        # 原文の日本語の引用にある仮名は訳した証拠にならない。原文をそのまま返した返答は翻訳失敗にし、
        # 引用を残して訳した返答は訳文として表示する。
        source = "The report adds a section named 「モデル別の集計」 and updates two files."
        self.use_child("\n".join([
            "import re, sys",
            "sys.stdin.read()",
            "prompt = open(sys.argv[sys.argv.index('--system-prompt-file') + 1], encoding='utf-8').read()",
            "print('<translation>\\n' + re.search(r'<source>\\n(.*)\\n</source>', prompt, re.S).group(1) + '\\n</translation>')",
            "",
        ]))
        self.assertEqual(self.flush(0, True, source, "echo-quote", "standalone"), FAIL_MARK + source)
        self.assertIn(" result=no_japanese ", self.log())
        translated = "「モデル別の集計」という節を追加し、2つのファイルを更新します。"
        self.use_child("print(%r)" % ("<translation>\n" + translated + "\n</translation>"))
        self.assertEqual(self.flush(0, True, source, "quote", "standalone"), MARK + translated)

    def test_child_error_is_classified_without_its_text_or_cooldown(self):
        self.use_child("import sys; print('SYNTHETIC_ERROR_DETAIL'); sys.exit(1)")
        self.assertIn("翻訳失敗", self.final_once("error"))
        self.assertNotIn("SYNTHETIC_ERROR_DETAIL", self.log())
        self.assertFalse((self.state / "last-failure").exists())

    def test_timeout_shows_the_original_quickly_and_starts_cooldown(self):
        self.use_child("import time; time.sleep(5)")
        self.env["MSGDISP_CHILD_TIMEOUT"] = "0.1"
        started = time.monotonic()
        self.assertIn("翻訳失敗", self.final_once("timeout"))
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue((self.state / "last-failure").is_file())

    def test_credentials_are_never_sent_shown_or_logged(self):
        secret = "sk" + "-ant-" + "synthetic-invalid-value"
        self.use_child("print(%r)" % secret)
        runtime = self.runtime()
        with patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(runtime["run_child"](secret))
            self.assertIsNone(runtime["run_child"]("Hello from the translator"))
        log = self.log()
        self.assertIn("credential_in_input", log)
        self.assertIn("credential_in_output", log)
        self.assertNotIn(secret, log)

    def test_short_source_keeps_its_correct_translation(self):
        # 非finalで言語確定前に表示した行の続きだけを訳す場合、原文は数文字になり得る。
        self.use_child("print('<translation>はい、そのとおりです。</translation>')")
        runtime = self.runtime()
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(runtime["translate"]("Yes.", ""), "はい、そのとおりです。")

    def test_a_stray_backtick_does_not_pair_with_inline_code(self):
        # 行中の ``` と後続の `ls` の間は英文なので、訳されて消えても訳文を表示する。`ls` が欠けた訳文は拒否する。
        source = "Close the ``` fence, then run `ls`."
        cases = (("``` フェンスを閉じてから `ls` を実行します。", "``` フェンスを閉じてから `ls` を実行します。"),
                 ("``` フェンスを閉じてから ls を実行します。", None))
        for translation, expected in cases:
            with self.subTest(translation=translation):
                self.use_child("print(%r)" % ("<translation>" + translation + "</translation>"))
                runtime = self.runtime()
                with patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual(runtime["translate"](source, ""), expected)

    def test_japanese_only_inline_code_may_lose_its_backticks(self):
        # 子CLIは `。` を「。」と書き直すことがあり、その訳文は表示する。中身ごと消えた訳文と、
        # ASCIIを含む inline code（空白だけ、日本語との混在）がバッククォートごと消えた訳文は拒否する。
        cases = (("Use `split()` to cut the text at each `。`.", "`split()` で文を「。」ごとに分けます。", True),
                 ("Print `完了` when it ends.", "終わったら表示します。", False),
                 ("Join the words with ` `.", "単語を空白でつなぎます。", False),
                 ("Open `設定.json` first.", "まず 設定.json を開きます。", False))
        for source, translation, shown in cases:
            with self.subTest(source=source):
                self.use_child("print(%r)" % ("<translation>" + translation + "</translation>"))
                runtime = self.runtime()
                with patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual(runtime["translate"](source, ""), translation if shown else None)

    def test_a_numbered_item_that_lost_its_space_gets_it_back(self):
        # 子CLIは `2.（後で）` のように番号直後の空白を落とすことがあり、その訳文は空白を補って表示する。
        # 全角数字の番号はリスト項目にならないので補わずに拒否し、`1.5倍` のような行は変えない。
        source = "1. Build it.\n2. (later) Ship it.\n\n1.5x faster."
        cases = (("1. ビルドする。\n2.（後で）出荷する。\n\n1.5倍速い。", "1. ビルドする。\n2. （後で）出荷する。\n\n1.5倍速い。"),
                 ("1. ビルドする。\n２.（後で）出荷する。\n\n1.5倍速い。", None))
        for translation, expected in cases:
            with self.subTest(translation=translation):
                self.use_child("print(%r)" % ("<translation>" + translation + "</translation>"))
                runtime = self.runtime()
                with patch.dict(os.environ, self.env, clear=True):
                    self.assertEqual(runtime["translate"](source, ""), expected)

    def test_timeout_starts_cooldown_even_when_the_kill_fails(self):
        # Popen を差し替えるので起動はしないが、claude が無い環境でも no_claude で止まらないよう起動コマンドを固定する。
        self.use_child("")
        for kill_error in (None, PermissionError()):
            with self.subTest(kill_error=type(kill_error).__name__):
                self.state = self.root / ("timeout-state-" + type(kill_error).__name__)
                self.state.mkdir()
                self.env["MSGDISP_STATE_DIR"] = str(self.state)
                runtime = self.runtime()
                with patch.dict(os.environ, self.env, clear=True), patch("subprocess.Popen") as launch, \
                        patch("os.killpg", side_effect=kill_error):
                    launch.return_value.communicate.side_effect = subprocess.TimeoutExpired("fake", 0.1)
                    launch.return_value.returncode = None
                    self.assertIsNone(runtime["run_child"]("Hello from the translator"))
                self.assertTrue((self.state / "last-failure").is_file())
                self.assertIn("timeout", self.log())


class TranslationFlowTest(HookTestCase):
    def test_replaces_held_english_lines_with_the_translation_on_the_final_flush(self):
        marker = self.use_translator()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertEqual(outputs[:2], [MARK, ""])
        self.assertIn("やく", outputs[2])
        for piece in ("`npm ci`", "`bun test`", "https://example.com/docs"):
            self.assertIn(piece, outputs[2])
        self.assertTrue(marker.exists())

    def test_shows_every_held_english_line_when_the_translation_command_fails(self):
        self.use_translator(exit_code=1)
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertEqual(outputs[:2], [MARK, ""])
        self.assertEqual(outputs[2], FAIL_MARK + "".join(ENGLISH))

    def test_keeps_translating_the_next_message_after_a_fast_failure(self):
        self.use_translator(exit_code=1, name="failing")
        self.run_flushes([*ENGLISH, ""], "message-1")
        marker = self.use_translator(name="working")
        outputs = self.run_flushes([*ENGLISH, ""], "message-2")
        self.assertTrue(marker.exists())
        self.assertEqual(outputs[0], MARK)
        self.assertIn("やく", outputs[2])

    def test_translates_a_message_that_names_reserved_tags(self):
        marker = self.use_translator()
        # 同じタグを2回含め、出現ごとに別のplaceholderへ移して戻すことを確かめる。
        outputs = self.run_flushes(["The hook hides `</source>`, a bare </source> and <source> before translating.\n", ""])
        self.assertTrue(marker.exists())
        self.assertIn("やく", outputs[1])
        self.assertIn("`</source>`, a bare </source> and <source> before", outputs[1])

    def test_leaves_japanese_messages_untouched_without_calling_the_translator(self):
        marker = self.use_translator()
        # 英字が翻訳対象の閾値を超える日本語にして、英字数ではなく日本語判定で素通しすることを確かめる。
        outputs = self.run_flushes([
            "Claude Code の MessageDisplay hook は settings.json の登録で動きます。\n",
            "次に `bun test` を実行してください。\n", "",
        ])
        self.assertEqual(outputs, [None, None, None])
        self.assertFalse(marker.exists())

    # 伏せ字だけの行（前にリストや見出しの記号があってもよい）はそのまま返し、ほかの行へカナを足す偽翻訳。
    # 受け取った原文を received に書き、日本語の行が翻訳に送られていないことを確かめられるようにする。
    def use_line_translator(self, received, swap=False):
        self.use_child("\n".join([
            "import re, sys",
            "prompt = open(sys.argv[sys.argv.index('--system-prompt-file') + 1], encoding='utf-8').read()",
            "source = re.search(r'<source>\\n(.*)\\n</source>', prompt, re.S).group(1)",
            "open(%r, 'w', encoding='utf-8').write(source)" % str(received),
            "hidden = r'[ \\t>#*+0-9.)-]*\\[\\[\\[CODE\\d+\\]\\]\\]'",
            "lines = [line if not line.strip() or re.fullmatch(hidden, line) else line + ' やく' for line in source.split('\\n')]",
            "if %r:" % swap,
            "    found = [i for i, line in enumerate(lines) if re.fullmatch(hidden, line)]",
            "    lines[found[0]], lines[found[1]] = lines[found[1]], lines[found[0]]",
            "print('<translation>\\n' + '\\n'.join(lines) + '\\n</translation>')",
            "",
        ]))

    def test_translates_only_the_english_lines_of_a_mostly_japanese_message(self):
        # 1行目だけで英語と確定して [自動翻訳] を表示した後に、全文がほぼ日本語だと分かった状況を固定する。
        # 日本語の行は翻訳に送らずに原文のまま表示し、インデントした行を含む英語の行だけを訳す。
        received = self.root / "received.txt"
        self.use_line_translator(received)
        outputs = self.run_flushes([ENGLISH[0], MOSTLY_JAPANESE, ""])
        expected = (ENGLISH[0].rstrip("\n") + " やく\n"
                    + MOSTLY_JAPANESE.replace("before saving.", "before saving. やく"))
        self.assertEqual(outputs, [MARK, "", expected])
        self.assertIsNone(re.search("[\u3040-\u30ff\u4e00-\u9fff]", received.read_text(encoding="utf-8")))
        self.assertIn("result=translated", self.log())

    def test_rejects_a_translation_that_reorders_the_hidden_japanese_lines(self):
        # 伏せ字の入れ替わりは各伏せ字が1回ずつ現れても起こり、そのまま戻すと日本語の行の順序が変わる。
        self.use_line_translator(self.root / "received.txt", swap=True)
        outputs = self.run_flushes([ENGLISH[0], MOSTLY_JAPANESE, ""])
        self.assertEqual(outputs[2], FAIL_MARK + ENGLISH[0] + MOSTLY_JAPANESE)

    def test_marks_a_mostly_japanese_message_with_no_english_line_to_translate(self):
        marker = self.use_translator()
        # 最初のflushが日本語の前で切れて英語と確定し、英語はその日本語の行の中にしか無い状況を固定する。
        # 日本語の行を伏せると訳す英語が残らないので、翻訳を呼ばずに印を付けて原文を表示する。
        deltas = ["Reviewing the pull request for the thinking note and README ",
                  "の変更点を確認し、問題なければマージします。\n",
                  "コミットをプッシュして、レビューもおわりました。あとはマージをまつだけです。\n", ""]
        outputs = self.run_flushes(deltas)
        self.assertEqual(outputs, [MARK, "", "", MOSTLY_JA_MARK + "".join(deltas)])
        self.assertFalse(marker.exists())
        self.assertIn("skipped_mostly_ja", self.log())

    def test_marks_a_mostly_japanese_message_whose_only_english_is_in_table_rows(self):
        # 日本語の行を伏せた後に残る英語が表の中の担当名やモデル名だけなら、訳す語が無いので翻訳を呼ばずに印を付ける。
        # 表の行を訳す英語に数えると翻訳に送り、訳文に仮名が増えないので翻訳失敗として原文を表示していた。
        marker = self.use_translator()
        deltas = ["Reviewing the pull request for the thinking note and README ",
                  "の変更点を確認し、問題なければマージします。\n",
                  "コミットをプッシュして、レビューもおわりました。あとはマージをまつだけです。\n\n"
                  "| 工程 | 担当 | モデル |\n|---|---|---|\n"
                  "| 調査 | worker | example-model-2 / high |\n| 実装 | executor | example-model-3 / medium |\n", ""]
        outputs = self.run_flushes(deltas)
        self.assertEqual(outputs, [MARK, "", "", MOSTLY_JA_MARK + "".join(deltas)])
        self.assertFalse(marker.exists())
        self.assertIn("skipped_mostly_ja", self.log())

    def test_does_not_repeat_a_line_shown_before_the_language_was_decided(self):
        self.use_translator()
        # 並行に起動したflushが逆順に処理され、短いflush 1が言語確定前に原文で表示された状況を固定する。
        shown = {}
        for index, final, delta in ((1, False, "Done.\n"), (0, False, ENGLISH[0]), (2, True, "")):
            output = self.flush(index, final, delta)
            shown[index] = delta if output is None else output
        screen = "".join(shown[i] for i in sorted(shown))
        self.assertEqual(screen.count("Done."), 1)
        self.assertIn("やく", screen)

    def test_keeps_japanese_lines_original_after_switching_to_english(self):
        # 日本語と判定した後に来た英語の行は、その位置から英語の経路に切り替えて訳す。
        # 切り替え後に英語が大半でも、日本語の行は翻訳に送らず原文のまま表示する。
        received = self.root / "received.txt"
        self.use_line_translator(received)
        first = "まずは変更内容を確認します。\n"
        paragraph = ("The build failed because the peer dependency was missing.\n"
                     "Run the test suite again after the lockfile update.\n")
        last = "確認はここまでです。\n"
        outputs = self.run_flushes([first, paragraph, last, ""])
        expected = paragraph.replace("\n", " やく\n") + last
        self.assertEqual(outputs, [None, MARK, "", expected])
        self.assertIsNone(re.search("[\u3040-\u30ff\u4e00-\u9fff]", received.read_text(encoding="utf-8")))

    def test_does_not_switch_languages_for_english_inside_a_code_block(self):
        # フェンスの中の英語では切り替えない。閉じていないフェンスの中も判定から外す。
        marker = self.use_translator()
        outputs = self.run_flushes([
            "まずは変更内容を確認します。\n```text\n",
            "The build failed because the peer dependency was missing.\n",
            "```\n確認はここまでです。\n",
            "",
        ])
        self.assertEqual(outputs, [None, None, None, None])
        self.assertFalse(marker.exists())

    def test_does_not_switch_languages_for_table_rows(self):
        # 担当名・モデル名だけの表の行で切り替えると、訳す語が無いうえ、印の行が表の行として表示される。
        # 表の前で flush が切れ、表直後の抑止を通らずに表の行だけで判定される状況を固定する。
        marker = self.use_translator()
        outputs = self.run_flushes([
            "工程ごとの担当は次の表のとおりです。\n\n",
            "| 工程 | 担当 | モデル |\n|---|---|---|\n"
            "| 調査 | worker | example-model-2 / high |\n| 実装 | executor | example-model-3 / medium |\n",
            "",
        ])
        self.assertEqual(outputs, [None, None, None])
        self.assertFalse(marker.exists())

    def test_does_not_switch_languages_while_a_table_continues(self):
        # 表が続く間に英語で切り替えると、印は表示済みの表に続いて表の行になる。空行の無い次の行も表の続きになる。
        # 英語が最後の flush で来る経路も確かめる。
        marker = self.use_translator()
        table = "工程ごとの担当は次の表のとおりです。\n\n| 工程 | 担当 |\n|---|---|\n| 設計 | 本体 |\n"
        english = "\nThe build failed because the peer dependency was missing.\n"
        for name, deltas in (("non-final", [table, english, ""]), ("final", [table, english]),
                             ("continued", [table, "補足です。\n", english, ""])):
            with self.subTest(name):
                outputs = self.run_flushes(deltas, message_id="after-table-" + name)
                self.assertEqual(outputs, [None] * len(deltas))
                self.assertFalse(marker.exists())

    def test_switches_to_english_on_a_single_final_flush(self):
        # Agent SDK 経由では、メッセージ全体が index 0 の final として一度に届く。
        received = self.root / "received.txt"
        self.use_line_translator(received)
        japanese = "実装が完了しました。\n"
        english = "Implementation complete. What would you like to do?\n"
        output = self.flush(0, True, japanese + english)
        self.assertEqual(output, MARK + japanese + english.rstrip("\n") + " やく\n")
        self.assertIsNone(re.search("[\u3040-\u30ff\u4e00-\u9fff]", received.read_text(encoding="utf-8")))

    def test_does_not_switch_languages_during_a_translation_cooldown(self):
        marker = self.use_translator()
        self.state.mkdir(exist_ok=True)
        (self.state / "last-failure").touch()
        outputs = self.run_flushes([
            "コミットをプッシュして、レビューもおわりました。\n",
            "Implementation complete. What would you like to do?\n",
            "",
        ])
        self.assertEqual(outputs, [None, None, None])
        self.assertFalse(marker.exists())


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=WAIT_SECONDS)


@unittest.skipUnless(os.access("/proc/locks", os.R_OK), "/proc/locks is Linux only")
class LockRaceTest(HookTestCase):
    def test_a_non_final_flush_that_loses_the_race_to_the_final_close_shows_its_line_once(self):
        self.use_translator()
        self.assertEqual(self.flush(0, False, ENGLISH[0]), MARK)
        self.assertIsNone(self.flush_while_closing(1, ENGLISH[1]))
        final = self.flush(2, True, "")
        self.assertIn("やく", final)
        self.assertNotIn("`bun test`", final)

    def test_an_undecided_flush_that_loses_the_race_to_the_final_close_leaves_no_seen_file(self):
        # 言語未確定の短い行。finalのcleanup後にseenを書くと、本文入りのファイルが次のGCまで残る。
        self.assertIsNone(self.flush_while_closing(0, "Done.\n"))
        self.assertEqual(list(self.state.glob("*.seen")), [])

    def flush_while_closing(self, index, delta):
        self.state.mkdir(exist_ok=True)
        # finalがmessage単位のlockを先に取ってclosedを作る順序を、外からlockを保持して固定する。
        key = str(self.state / "session1-message-1")
        holder = subprocess.Popen([sys.executable, "-c", "\n".join([
            "import fcntl, sys, time",
            "handle = open(sys.argv[1], 'ab')",
            "fcntl.flock(handle, fcntl.LOCK_EX)",
            "print('locked', flush=True)",
            "time.sleep(60)",
        ]), key + ".lock"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(stop, holder)
        ready, _, _ = select.select([holder.stdout], [], [], WAIT_SECONDS)
        self.assertTrue(ready, "lock holder did not start")
        self.assertEqual(holder.stdout.readline(), "locked\n")
        # 閉じたstdinへのcommunicateは古いPythonでValueErrorになるため、入力はファイルから渡す。
        request = self.root / "flush.json"
        request.write_text(payload("message-1", index, False, delta), encoding="utf-8")
        with request.open(encoding="utf-8") as stdin:
            waiting = subprocess.Popen([sys.executable, "-B", str(HOOK)], stdin=stdin, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=self.env, text=True)
        self.addCleanup(stop, waiting)
        # 非finalが最初のclosed確認を通過してlock待ちに入ったことを /proc/locks の待機行で確かめる。
        # lockを使わない実装はここで待たずに終了するので、そのままclosed作成へ進む。
        blocked = re.compile(r"-> FLOCK +ADVISORY +WRITE +%d " % waiting.pid)
        deadline = time.monotonic() + WAIT_SECONDS
        while waiting.poll() is None and not blocked.search(Path("/proc/locks").read_text()):
            self.assertLess(time.monotonic(), deadline, "hook did not wait for the message lock")
            time.sleep(0.01)
        Path(key + ".closed").write_text("")
        holder.kill()
        holder.wait(timeout=WAIT_SECONDS)
        stdout, _ = waiting.communicate(timeout=WAIT_SECONDS)
        self.assertEqual(waiting.returncode, 0)
        return displayed(stdout)


class AttendedTest(HookTestCase):
    # 非対話セッションでは翻訳しない。ただしOrcaのチャット画面はSDK経由でも人が画面を見ているので翻訳する。
    # チャット内のBashツールから起動した入れ子のclaude -pは、スクリプトが出力を読むので翻訳しない。
    ORCA_CHAT = ("1 0 /sbin/launchd\n200 1 /Applications/Orca.app/Contents/MacOS/Orca Helper\n"
                 "300 200 /Users/u/.local/bin/claude\n")
    NESTED = ORCA_CHAT + "400 300 /bin/zsh\n500 400 /Users/u/.local/bin/claude\n"
    # 外側のclaudeとプロセス名が異なる入れ子。外側はClaude Codeの実行中セッションの記録で見分ける。
    NESTED_RENAMED = ORCA_CHAT + "400 300 /bin/zsh\n500 400 /opt/node/bin/node\n"
    BROKEN_CHAIN = "1 0 /sbin/launchd\n300 200 /Users/u/.local/bin/claude\n"

    def setUp(self):
        super().setUp()
        for name in ("MSGDISP_FORCE_ATTENDED", "ORCA_STRUCTURED_SESSION"):
            self.env.pop(name, None)
        self.env["CLAUDE_CODE_SESSION_ATTENDED"] = "0"
        # 開発者の手元で実行中のClaude Codeのセッションの記録を拾わないようにする。
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "claude-config")

    def use_ps(self, listing, claude_pid, in_orca_chat=True, recorded=()):
        # listingがNoneなら、psが失敗する環境を表す。recordedは、Claude Codeが実行中のセッションとして記録したPID。
        sessions = Path(self.env["CLAUDE_CONFIG_DIR"]) / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        for pid in recorded:
            (sessions / (pid + ".json")).write_text("{}", encoding="utf-8")
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        ps = bin_dir / "ps"
        ps.write_text("#!/bin/sh\n" + ("exit 1\n" if listing is None else "cat <<'EOF'\n" + listing + "EOF\n"),
                      encoding="utf-8")
        ps.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["CLAUDE_PID"] = claude_pid
        if in_orca_chat:
            self.env["ORCA_STRUCTURED_SESSION"] = "1"

    def test_sessions_nobody_watches_are_left_untouched(self):
        # 同じ名前の入れ子は記録なしでも見分け、名前が異なる入れ子は記録で見分ける。
        cases = {"outside-orca": (self.ORCA_CHAT, "300", False, ()),
                 "nested-claude-p": (self.NESTED, "500", True, ()),
                 "nested-claude-p-with-another-name": (self.NESTED_RENAMED, "500", True, ("300",)),
                 "ancestor-missing": (self.BROKEN_CHAIN, "300", True, ()),
                 "ps-unavailable": (None, "300", True, ())}
        base = self.env
        for message_id, (listing, claude_pid, in_orca_chat, recorded) in cases.items():
            with self.subTest(message_id):
                self.env = dict(base, CLAUDE_CONFIG_DIR=str(self.root / message_id))
                self.use_ps(listing, claude_pid, in_orca_chat, recorded)
                marker = self.use_translator(name=message_id)
                self.assertIsNone(self.final_once(message_id))
                self.assertFalse(marker.exists())

    def test_an_orca_chat_session_is_translated(self):
        self.use_ps(self.ORCA_CHAT, "300")
        marker = self.use_translator()
        self.assertEqual(self.final_once("orca-chat"), MARK + SOURCE + " やく")
        self.assertTrue(marker.exists())


class StateDirectoryTest(HookTestCase):
    # 状態ファイルには表示を保留した本文が入るので、他ユーザーから読めない権限を保証する。
    def test_an_existing_state_directory_is_made_owner_only(self):
        self.state.mkdir(mode=0o755)
        os.chmod(self.state, 0o755)
        self.use_translator()
        self.run_flushes([*ENGLISH, ""])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
