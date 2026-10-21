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
SOURCE = "Approved. Committing, pushing, and updating the PR body."
ENGLISH = [
    "The build failed because `npm ci` could not resolve a peer dependency.\n",
    "Run `bun test` and read https://example.com/docs for details.\n",
]
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

    def test_timeout_starts_cooldown_even_when_the_kill_fails(self):
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


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=WAIT_SECONDS)


@unittest.skipUnless(os.access("/proc/locks", os.R_OK), "/proc/locks is Linux only")
class LockRaceTest(HookTestCase):
    def test_a_non_final_flush_that_loses_the_race_to_the_final_close_shows_its_line_once(self):
        self.use_translator()
        self.assertEqual(self.flush(0, False, ENGLISH[0]), MARK)
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
        request = self.root / "flush-1.json"
        request.write_text(payload("message-1", 1, False, ENGLISH[1]), encoding="utf-8")
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
        self.assertIsNone(displayed(stdout))
        final = self.flush(2, True, "")
        self.assertIn("やく", final)
        self.assertNotIn("`bun test`", final)


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
