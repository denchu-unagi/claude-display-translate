import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
from unittest import mock
import unittest

from test_display_translate import ENGLISH, FAIL_MARK, HOOK, HookTestCase, MARK, displayed, payload

BUNDLE = Path(__file__).resolve().parent.parent / "hooks" / "claude-display-translate"
TRANSLATORS = BUNDLE / "translators"
SETTINGS_SAMPLE = BUNDLE / "settings.py.sample"

# 訳文の代わりに、原文の各行へ仮名を足して返す。hook の忠実性検査を通る形に保つ。
ECHO_BODY = [
    "    source = re.search(r'<source>\\n(.*)\\n</source>', prompt, re.S).group(1)",
    "    lines = [line + ' やく' if line else line for line in source.split('\\n')]",
    "    return '<translation>\\n' + '\\n'.join(lines) + '\\n</translation>'",
]


class ModelFileTest(HookTestCase):
    def setUp(self):
        super().setUp()
        self.config = self.root / "config"
        (self.config / "translators").mkdir(parents=True)
        self.env["MSGDISP_CONFIG_DIR"] = str(self.config)
        # 設定ファイルを使うときは既定の claude を呼ばないことを、失敗する偽 claude で確かめる。
        self.use_child("import sys; sys.exit(3)")

    def use_settings(self, model, extra=""):
        (self.config / "settings.py").write_text("MODEL = %r\n%s" % (model, extra), encoding="utf-8")

    def use_model(self, body, name="custom", extra=""):
        (self.config / "translators" / (name + ".py")).write_text(body, encoding="utf-8")
        self.use_settings(name, extra)

    def use_bundled_cli(self, cli_suffix=""):
        # 同梱の _cli の run_cli で、CLAUDE_CODE_EXECPATH の偽 CLI を起動するモデルを置く。
        target = self.config / "translators"
        (target / "_cli.py").write_text((TRANSLATORS / "_cli.py").read_text(encoding="utf-8") + cli_suffix,
                                        encoding="utf-8")
        self.use_model("import os\nfrom _cli import run_cli\ndef translate(prompt, timeout, settings):\n"
                       "    return run_cli([os.environ['CLAUDE_CODE_EXECPATH']], '', timeout)\n", name="cli-model")

    def use_fake_claude(self, pid_file):
        fake = self.root / "fake-claude"
        fake.write_text("#!%s\nimport os, time\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(30)\n"
                        % (sys.executable, str(pid_file)), encoding="utf-8")
        fake.chmod(0o755)
        self.env["CLAUDE_CODE_EXECPATH"] = str(fake)

    def assert_process_ends(self, pid_file):
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            # SIGKILL 直後は init に回収されるまで zombie として残るので、それも停止とみなす。
            if state == "Z":
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail("process %d is still running" % pid)

    def assert_shows_original(self, outputs, reason):
        self.assertTrue(outputs[2].startswith(FAIL_MARK), outputs[2])
        self.assertIn("npm ci", outputs[2])
        self.assertIn(reason, self.log())

    def test_without_settings_the_default_claude_is_used(self):
        self.env["MSGDISP_CONFIG_DIR"] = str(self.root / "missing")
        self.use_translator()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertIn("やく", outputs[2])

    def test_the_model_file_named_in_settings_replaces_the_claude_child(self):
        marker = self.root / "called"
        self.env["CLAUDECODE"] = "1"
        self.use_model("\n".join([
            "import os, re",
            "def translate(prompt, timeout, settings):",
            "    open(%r, 'w').close()" % str(marker),
            "    assert prompt.startswith('You translate Markdown text')",
            "    assert os.environ['MSGDISP_TRANSLATE_CHILD'] == '1'",
            "    assert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'",
            "    assert 'CLAUDECODE' not in os.environ",
            "    assert 0 < timeout <= 55",
            "    assert settings.EXTRA == 'value'",
            *ECHO_BODY,
            "",
        ]), extra="EXTRA = 'value'\n")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertEqual(outputs[:2], [MARK, ""])
        self.assertIn("やく", outputs[2])
        self.assertIn("`npm ci`", outputs[2])
        self.assertTrue(marker.exists())

    def test_the_haiku_model_uses_the_builtin_haiku(self):
        marker = self.use_translator()
        # translators/haiku.py を置いても読まないことを、読まれたら失敗するファイルで確かめる。
        self.use_model("raise SystemExit(5)\n", name="haiku")
        self.use_settings(" haiku ")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertIn("やく", outputs[2])
        self.assertTrue(marker.exists())

    def test_the_model_name_may_have_hyphens_and_surrounding_whitespace(self):
        self.use_model("import re\ndef translate(prompt, timeout, settings):\n" + "\n".join(ECHO_BODY) + "\n",
                       name="my-model-2")
        self.use_settings(" my-model-2 \n")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertIn("やく", outputs[2])

    def test_a_bad_or_missing_model_shows_the_original(self):
        # MODEL で translators の外のファイルを指定できないことを、読まれたら印を残すファイルで確かめる。
        outside = self.root / "outside-called"
        (self.config / "evil.py").write_text(
            "open(%r, 'w').close()\ndef translate(prompt, timeout, settings):\n    return ''\n" % str(outside),
            encoding="utf-8")
        for name, model in (("traversal", "../evil"), ("upper", "Custom"), ("missing", "nothere"), ("empty", ""),
                            ("not_text", 3)):
            with self.subTest(name=name):
                self.state = self.root / ("state-" + name)
                self.env["MSGDISP_STATE_DIR"] = str(self.state)
                self.use_settings(model)
                outputs = self.run_flushes([*ENGLISH, ""], message_id="message-" + name)
                self.assert_shows_original(outputs, "translator_error")
                self.assertFalse(outside.exists())

    def test_a_model_file_linked_outside_translators_is_rejected(self):
        # MODEL が指すファイルの実体（symlink 経由を含む）が translators の外なら実行しない。
        outside = self.root / "outside-called"
        target = self.root / "outside.py"
        target.write_text("open(%r, 'w').close()\ndef translate(prompt, timeout, settings):\n    return ''\n"
                          % str(outside), encoding="utf-8")
        (self.config / "translators" / "custom.py").symlink_to(target)
        self.use_settings("custom")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assert_shows_original(outputs, "translator_error")
        self.assertFalse(outside.exists())

    def test_an_existing_bytecode_cache_is_not_used(self):
        # 同じサイズ・同じ mtime の書き換えでも、ソースの隣に残った古い .pyc ではなく今のソースを使う。
        import py_compile
        translators = self.config / "translators"
        cli = translators / "_cli.py"
        source = "def answer():\n    return '<translation>\\n%s\\n</translation>'\n"
        cli.write_text(source % "ああ", encoding="utf-8")
        mtime_ns = cli.stat().st_mtime_ns
        py_compile.compile(str(cli), cfile=str(translators / "__pycache__" / ("_cli." + sys.implementation.cache_tag + ".pyc")),
                           doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        cli.write_text(source % "いい", encoding="utf-8")
        os.utime(cli, ns=(mtime_ns, mtime_ns))
        self.use_model("from _cli import answer\ndef translate(prompt, timeout, settings):\n    return answer()\n")
        output = self.flush(0, True, "Approved. Committing, pushing, and updating the PR body.", "message-cache", "cache")
        self.assertIn("いい", output)

    def test_a_failing_model_file_shows_the_original(self):
        bodies = {
            "raises": "def translate(prompt, timeout, settings):\n    raise ValueError('boom')\n",
            "syntax": "def translate(prompt, timeout, settings)\n",
            "missing": "VALUE = 1\n",
            "not_text": "def translate(prompt, timeout, settings):\n    return None\n",
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.state = self.root / ("state-" + name)
                self.env["MSGDISP_STATE_DIR"] = str(self.state)
                self.use_model(body)
                outputs = self.run_flushes([*ENGLISH, ""], message_id="message-" + name)
                self.assert_shows_original(outputs, "translator_error")
                self.assertNotIn("boom", self.log())
                self.assertFalse((self.state / "last-failure").exists())

    def test_a_broken_settings_file_shows_the_original(self):
        (self.config / "settings.py").write_text("MODEL = \n", encoding="utf-8")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assert_shows_original(outputs, "translator_error")

    def test_translator_timeout_starts_cooldown(self):
        self.use_model("def translate(prompt, timeout, settings):\n    raise TimeoutError()\n")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assert_shows_original(outputs, "timeout")
        self.assertTrue((self.state / "last-failure").is_file())

    def test_a_translator_that_overruns_its_timeout_is_cut_off(self):
        # 設定ファイルの関数が止まらなくても、Claude Code の hook timeout より前に原文へ戻し cooldown に入る。
        self.env["MSGDISP_CHILD_TIMEOUT"] = "1"
        self.use_model("import time\ndef translate(prompt, timeout, settings):\n    time.sleep(30)\n")
        started = time.monotonic()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertLess(time.monotonic() - started, 10)
        self.assert_shows_original(outputs, "timeout")
        self.assertTrue((self.state / "last-failure").is_file())

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_a_stuck_translator_and_its_child_are_stopped(self):
        # 子が hook の stdout/stderr を継承したまま止まらなくても、期限で子ごと止めて原文へ戻す。
        pid_file = self.root / "child.pid"
        self.env["MSGDISP_CHILD_TIMEOUT"] = "1"
        self.use_model("\n".join([
            "import subprocess, sys, time",
            "def translate(prompt, timeout, settings):",
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "    open(%r, 'w').write(str(child.pid))" % str(pid_file),
            "    time.sleep(30)",
            "",
        ]))
        started = time.monotonic()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertLess(time.monotonic() - started, 10)
        self.assert_shows_original(outputs, "timeout")
        self.assertTrue((self.state / "last-failure").is_file())
        self.assert_process_ends(pid_file)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_the_bundled_run_cli_stops_its_cli_on_timeout(self):
        pid_file = self.root / "claude.pid"
        self.use_fake_claude(pid_file)
        self.use_bundled_cli()
        self.env["MSGDISP_CHILD_TIMEOUT"] = "1"
        started = time.monotonic()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertLess(time.monotonic() - started, 10)
        self.assert_shows_original(outputs, "timeout")
        self.assert_process_ends(pid_file)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_the_bundled_cli_is_stopped_when_the_hook_deadline_comes_first(self):
        # 同梱の run_cli の期限を延ばし、hook の期限が先に来る経路を同梱のコードで再現する。
        pid_file = self.root / "claude.pid"
        self.use_fake_claude(pid_file)
        self.use_bundled_cli("\n".join([
            "",
            "_run_cli = run_cli",
            "def run_cli(argv, stdin_text, timeout, cwd=None, env=None):",
            "    return _run_cli(argv, stdin_text, timeout + 30, cwd=cwd, env=env)",
            "",
        ]))
        self.env["MSGDISP_CHILD_TIMEOUT"] = "1"
        started = time.monotonic()
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertLess(time.monotonic() - started, 10)
        self.assert_shows_original(outputs, "timeout")
        self.assert_process_ends(pid_file)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_children_left_by_a_successful_translator_are_stopped(self):
        pid_file = self.root / "child.pid"
        self.use_model("\n".join([
            "import re, subprocess, sys",
            "def translate(prompt, timeout, settings):",
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "    open(%r, 'w').write(str(child.pid))" % str(pid_file),
            *ECHO_BODY,
            "",
        ]))
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertIn("やく", outputs[2])
        self.assert_process_ends(pid_file)

    def test_the_translator_temp_directory_is_removed(self):
        # 強制停止でも一時ファイル（原文など）を残さないよう、hook が一時領域を用意して消す。
        for name, tail in (("returns", "    return '<translation>\\n' + 'やく' + '\\n</translation>'"),
                           ("stuck", "    time.sleep(30)")):
            with self.subTest(name=name):
                record = self.root / ("tmpdir-" + name)
                self.state = self.root / ("state-" + name)
                self.env.update(MSGDISP_STATE_DIR=str(self.state), MSGDISP_CHILD_TIMEOUT="1")
                self.use_model("\n".join([
                    "import os, tempfile, time",
                    "def translate(prompt, timeout, settings):",
                    "    directory = tempfile.mkdtemp()",
                    "    open(os.path.join(directory, 'prompt.md'), 'w').write(prompt)",
                    "    open(%r, 'w').write(tempfile.gettempdir())" % str(record),
                    tail,
                    "",
                ]))
                self.run_flushes([*ENGLISH, ""], message_id="message-" + name)
                used = record.read_text()
                self.assertNotEqual(used, tempfile.gettempdir())
                self.assertFalse(Path(used).exists())

    def test_prints_in_the_translator_do_not_break_the_result(self):
        self.use_model("\n".join([
            "import re",
            "print('model loaded')",
            "def translate(prompt, timeout, settings):",
            "    print('debug output')",
            *ECHO_BODY,
            "",
        ]), extra="print('settings loaded')\n")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assertIn("やく", outputs[2])

    def test_modules_in_the_working_directory_do_not_shadow_the_standard_library(self):
        # Claude Code は hook を project のディレクトリで起動するので、そこの json.py などを読み込まない。
        workdir = self.root / "project"
        workdir.mkdir()
        (workdir / "json.py").write_text("raise RuntimeError('shadowed')\n", encoding="utf-8")
        self.use_model("import json, re\ndef translate(prompt, timeout, settings):\n" + "\n".join(ECHO_BODY) + "\n")
        deltas = [*ENGLISH, ""]
        last = None
        for index, delta in enumerate(deltas):
            result = subprocess.run([sys.executable, "-B", str(HOOK)],
                                    input=payload("message-1", index, index == len(deltas) - 1, delta),
                                    capture_output=True, text=True, env=self.env, cwd=workdir, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            last = displayed(result.stdout)
        self.assertIn("やく", last)

    def test_translator_output_still_goes_through_the_hook_checks(self):
        self.use_model("def translate(prompt, timeout, settings):\n    return 'ただの文章です。'\n")
        outputs = self.run_flushes([*ENGLISH, ""])
        self.assert_shows_original(outputs, "untagged")

    def test_credentials_are_not_sent_to_the_translator(self):
        marker = self.root / "called"
        self.use_model("def translate(prompt, timeout, settings):\n    open(%r, 'w').close()\n    return ''\n"
                       % str(marker))
        secret = "sk" + "-ant-" + "synthetic-invalid-value"
        runtime = self.runtime()
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(runtime["run_child"]("Hello " + secret))
        self.assertFalse(marker.exists())
        self.assertIn("credential_in_input", self.log())

    def test_credentials_in_the_translation_are_not_shown(self):
        secret = "sk" + "-ant-" + "synthetic-invalid-value"
        self.use_model("def translate(prompt, timeout, settings):\n    return %r\n"
                       % ("<translation>\n" + secret + " やく\n</translation>"))
        runtime = self.runtime()
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertIsNone(runtime["run_child"]("Hello from the translator"))
        self.assertIn("credential_in_output", self.log())
        self.assertNotIn(secret, self.log())

    def test_settings_and_model_files_leave_no_bytecode_cache(self):
        # 利用者の設定のディレクトリに __pycache__ を作らない。
        self.use_model("import re\ndef translate(prompt, timeout, settings):\n" + "\n".join(ECHO_BODY) + "\n")
        self.run_flushes([*ENGLISH, ""])
        self.assertEqual(list(self.config.rglob("*.pyc")), [])

    def test_a_settings_change_is_used_by_the_next_message(self):
        # 同じ秒・同じ長さの書き換えでも、古いキャッシュではなく今の内容で翻訳先を選ぶ。
        for name, word in (("aaaa", "あああ"), ("bbbb", "いいい")):
            (self.config / "translators" / (name + ".py")).write_text(
                "def translate(prompt, timeout, settings):\n    return %r\n" % ("<translation>\n" + word + "\n</translation>"),
                encoding="utf-8")
        outputs = []
        for index, name in enumerate(("aaaa", "bbbb")):
            self.use_settings(name)
            outputs.append(self.flush(0, True, "Approved. Committing, pushing, and updating the PR body.",
                                      "message-%d" % index, "settings-change"))
        self.assertIn("あああ", outputs[0])
        self.assertIn("いいい", outputs[1])


def load(name, path):
    # settings.py.sample のように .py 以外の名前のファイルも読むため、loader を明示する。
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


PROMPT = "Instructions.\n\n<source>\nHello [[[CODE1000]]]\n</source>"


class BundledTest(unittest.TestCase):
    def setUp(self):
        # モデルのファイルは `from _cli import ...` で共通部を読むので、先に _cli を登録しておく。
        self.cli = load("_cli", TRANSLATORS / "_cli.py")
        patcher = mock.patch.dict(sys.modules, {"_cli": self.cli})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_settings_sample_defaults_to_haiku(self):
        settings = load("settings_sample_check", SETTINGS_SAMPLE)
        self.assertEqual(settings.MODEL, "haiku")
        self.assertEqual(sorted(p.stem for p in TRANSLATORS.glob("[!_]*.py")), ["luna", "sarashina"])

    def test_run_cli_passes_stdin_and_stops_on_timeout(self):
        echo = [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"]
        self.assertEqual(self.cli.run_cli(echo, "abc", 10).strip(), "ABC")
        sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(TimeoutError):
            self.cli.run_cli(sleeper, "", 0.5)
        failing = [sys.executable, "-c", "import sys; sys.exit(4)"]
        with self.assertRaises(RuntimeError):
            self.cli.run_cli(failing, "", 10)

    def test_run_cli_returns_even_when_the_kill_fails(self):
        sleeper = [sys.executable, "-c", "import time; time.sleep(5)"]
        started = time.monotonic()
        with mock.patch.object(self.cli.subprocess.Popen, "kill", side_effect=PermissionError()), \
                self.assertRaises(TimeoutError):
            self.cli.run_cli(sleeper, "", 0.5)
        self.assertLess(time.monotonic() - started, 4)

    def test_run_cli_keeps_the_cli_in_the_callers_process_group(self):
        # hook が設定ファイルのプロセスグループごと止めたとき、起動した CLI も止まるようにする。
        reported = self.cli.run_cli([sys.executable, "-c", "import os; print(os.getpgrp())"], "", 10)
        self.assertEqual(int(reported), os.getpgrp())

    def test_luna_calls_codex_with_the_prompt_in_a_file(self):
        calls = []

        def run_cli(argv, stdin_text, timeout, cwd=None, env=None):
            path = json.loads(next(a for a in argv if a.startswith("model_instructions_file=")).split("=", 1)[1])
            calls.append((argv, stdin_text, timeout, Path(path).read_text(encoding="utf-8")))
            return json.dumps({"type": "item.completed",
                               "item": {"type": "agent_message", "text": "<translation>\nやく\n</translation>"}}) + "\n"

        with mock.patch.object(self.cli, "run_cli", run_cli):
            luna = load("luna_check", TRANSLATORS / "luna.py")
            self.assertIn("やく", luna.translate(PROMPT, 12, types.SimpleNamespace()))
        (argv, stdin_text, timeout, prompt), = calls
        self.assertIn("gpt-6-luna", argv)
        self.assertEqual((stdin_text, timeout), ("Translate the source.", 12))
        # argv は /proc から他のユーザーにも読めるので、原文を載せない。
        self.assertEqual(prompt, PROMPT)
        self.assertFalse(any("Hello" in arg for arg in argv))

    def load_sarashina(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        key_file = Path(directory.name) / "api-key"
        key_file.write_text("test-key-0123\n", encoding="utf-8")
        return load("sarashina_check", TRANSLATORS / "sarashina.py"), types.SimpleNamespace(SARASHINA_API_KEY_FILE=str(key_file))

    def test_sarashina_drafts_reviews_and_polishes(self):
        sarashina, settings = self.load_sarashina()
        requests = []
        replies = [{"translated_text": "こんにちは [[[CODE1000]]]  "},
                   {"choices": [{"message": {"content": "<translation>\nしょうごう\n</translation>"}}]},
                   {"choices": [{"message": {"content": "<translation>\nやく\n</translation>"}}]}]

        def urlopen(request, timeout):
            requests.append((request.get_header("Authorization"), json.loads(request.data), timeout))
            return io.BytesIO(json.dumps(replies[len(requests) - 1]).encode("utf-8"))

        with mock.patch.object(sarashina.urllib.request, "urlopen", urlopen):
            self.assertEqual(sarashina.translate(PROMPT, 12, settings), "<translation>\nやく\n</translation>")
        (authorization, draft_body, _), (_, review_body, _), (_, polish_body, _) = requests
        # キーのファイルの末尾の改行は落とし、翻訳 API には指示を含めず原文だけを送る。
        self.assertEqual(authorization, "Bearer test-key-0123")
        self.assertEqual(draft_body, {"text": "Hello [[[CODE1000]]]", "source_lang": "en", "target_lang": "ja"})
        self.assertEqual(review_body["messages"][0]["content"], sarashina.SARASHINA_REVIEW_PROMPT)
        self.assertIn("<draft>\nこんにちは [[[CODE1000]]]\n</draft>", review_body["messages"][1]["content"])
        self.assertEqual(polish_body["messages"][0]["content"], sarashina.SARASHINA_POLISH_PROMPT)
        self.assertIn("<draft>\nしょうごう\n</draft>", polish_body["messages"][1]["content"])
        for body in (review_body, polish_body):
            self.assertEqual((body["model"], body["temperature"]), ("sarashina3-mini", 0.1))

    def test_sarashina_passes_the_remaining_time_and_stops_when_it_runs_out(self):
        sarashina, settings = self.load_sarashina()
        timeouts = []
        replies = [{"translated_text": "やく"}, {"choices": [{"message": {"content": "<translation>\nやく\n</translation>"}}]}]

        def urlopen(request, timeout):
            timeouts.append(timeout)
            return io.BytesIO(json.dumps(replies[len(timeouts) - 1]).encode("utf-8"))

        # 開始、照合の前、文体の整えの前に時計を読む。照合のあとで12秒を使い切ったことにする。
        with mock.patch.object(sarashina.urllib.request, "urlopen", urlopen), \
                mock.patch.object(sarashina.time, "monotonic", side_effect=[100.0, 103.0, 112.0]), \
                self.assertRaises(TimeoutError):
            sarashina.translate(PROMPT, 12, settings)
        self.assertEqual(timeouts, [12, 9.0])

    def test_sarashina_does_not_send_a_source_that_contains_the_key(self):
        sarashina, settings = self.load_sarashina()
        prompt = "Instructions.\n\n<source>\nThe key is test-key-0123\n</source>"
        with mock.patch.object(sarashina.urllib.request, "urlopen", side_effect=AssertionError("sent")), \
                self.assertRaises(RuntimeError):
            sarashina.translate(prompt, 12, settings)

    def test_sarashina_reports_a_connect_timeout_as_a_timeout(self):
        # hook は TimeoutError だけを時間切れとして扱い、翻訳を休む。
        sarashina, settings = self.load_sarashina()
        with mock.patch.object(sarashina.urllib.request, "urlopen", side_effect=urllib.error.URLError(TimeoutError())), \
                self.assertRaises(TimeoutError):
            sarashina.translate(PROMPT, 12, settings)


if __name__ == "__main__":
    unittest.main()
