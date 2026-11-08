import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from test_display_translate import HOOK, MARK

REPO = Path(__file__).resolve().parent.parent
TRANSLATORS = REPO / "hooks" / "claude-display-translate" / "translators"
MODEL = os.environ.get("MSGDISP_LIVE_MODEL", "haiku")
TIMEOUT_SECONDS = 180
# Claude にそのまま返させる英文。1つ目は、翻訳 API が括弧の中の inline code を括弧ごと落とした回帰の例で、MSGDISP_LIVE_MODEL=sarashina のときに意味を持つ。
ENGLISH = [
    "Status: the repo is public (`owner/repo`), so the private PR flow doesn't apply. "
    "Switching to the public-repo PR procedure.",
    "Run `make test` before you push, and read https://example.com/guide for the details.",
]
INSTRUCTION = "Reply with exactly the following line and nothing else, no code fence: "


def inline_codes(text):
    return re.findall(r"`[^`\n]+`", text)


def urls(text):
    return re.findall(r"https?://[^\s`]+", text)


def descendants(root):
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                                 timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2:
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, pending = [], [root]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


@unittest.skipUnless(os.environ.get("MSGDISP_LIVE") == "1",
                     "実際の Claude Code とネットワークを使うため、MSGDISP_LIVE=1 のときだけ実行する")
class LiveSessionTest(unittest.TestCase):
    def test_display_content_used(self):
        for text in ENGLISH:
            with self.subTest(text=text):
                self.check_english(text)

    def check_english(self, text):
        root = Path(tempfile.mkdtemp(prefix="claude-msgdisp-live-"))
        kept = []
        self.addCleanup(lambda: kept or shutil.rmtree(root, ignore_errors=True))
        config = root / "config"
        (config / "translators").mkdir(parents=True)
        for source in sorted(TRANSLATORS.glob("*.py")):
            shutil.copyfile(source, config / "translators" / source.name)
        (config / "settings.py").write_text(f'MODEL = "{MODEL}"\n', encoding="utf-8")
        state = root / "state"
        state.mkdir()
        work = root / "cwd"
        work.mkdir()
        # ユーザーの設定・hook・MCP・ツールを読まず、リポジトリの hook だけを通す。
        settings = json.dumps({
            # timeout は README の導入手順と同じ60秒にする。省くと Claude Code が10秒で hook を打ち切る。
            "hooks": {"MessageDisplay": [{"hooks": [{"type": "command", "command": shlex.quote(str(HOOK)), "timeout": 60}]}]},
            "disableClaudeAiConnectors": True,
            "autoMemoryEnabled": False,
        })
        command = [
            "claude", "-p",
            "--setting-sources", "",
            "--settings", settings,
            "--strict-mcp-config",
            "--no-session-persistence",
            "--tools", "",
            "--disable-slash-commands",
            "--model", "claude-haiku-5-5",
            "--output-format", "stream-json",
            "--verbose",
            "--include-hook-events",
            "--include-partial-messages",
            INSTRUCTION + text,
        ]
        # MSGDISP_CLAUDE_CMD などが残っていると、実際のモデルを呼ばずに通ってしまうため、hook 用の変数は引き継がない。
        env = {key: value for key, value in os.environ.items() if not key.startswith("MSGDISP_")}
        env.update(MSGDISP_FORCE_ATTENDED="1", MSGDISP_STATE_DIR=str(state), MSGDISP_CONFIG_DIR=str(config))
        # 端末の Ctrl-C を外側の claude に届けず、止める前に子孫をたどれるようにする。
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=str(work), env=env,
                                   start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.stop_tree(process, root, kept)
            self.fail(f"[timeout] {TIMEOUT_SECONDS}秒で終わらなかった\ninstruction: {INSTRUCTION + text!r}")
        except BaseException:
            self.stop_tree(process, root, kept)
            raise

        written = []
        display = None
        result = None
        for line in stdout.decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            # assistant と result は hook の訳文に置き換わって届くので、原文はストリームの差分から組み立てる。
            if kind == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta":
                    written.append(delta.get("text") or "")
            elif (kind == "system" and event.get("subtype") == "hook_response"
                  and event.get("hook_event") == "MessageDisplay"):
                try:
                    display = json.loads(event.get("output") or "")["hookSpecificOutput"]["displayContent"]
                except (KeyError, TypeError, ValueError):
                    display = None
            elif kind == "result":
                result = event.get("result")

        if process.returncode != 0:
            self.fail(f"[exit] claude の終了コードが {process.returncode} だった\n"
                      f"stderr: {stderr.decode('utf-8', 'replace')}")
        seen = "".join(written).strip()
        if seen != text:
            self.fail("[source] 翻訳より前に Claude が英文をそのまま返さなかった\n"
                      f"expected: {text!r}\nactual: {seen!r}")
        if display is None or not display.startswith(MARK):
            self.fail(f"[display] MessageDisplay hook の displayContent が {MARK!r} で始まらない\n"
                      f"displayContent: {display!r}")
        missing = [piece for piece in inline_codes(text) + urls(text) if piece not in display]
        if missing:
            self.fail("[display] displayContent が原文の inline code か URL を落としている\n"
                      f"missing: {missing!r}\ndisplayContent: {display!r}")
        if display.strip() != (result or "").strip():
            self.fail("[result] 最終出力が hook の訳文と一致しない\n"
                      f"displayContent: {display!r}\nresult: {result!r}")
        log = state / "translate.log"
        log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if not any("result=translated" in line for line in log_text.splitlines()):
            self.fail("[log] translate.log に result=translated の行がない\n"
                      f"log: {log_text!r}")

    def stop_tree(self, process, root, kept):
        # hook は翻訳を別の session で起動するので、外側の group だけでは止まらない。親を止めると親子関係が切れるため、先に子孫をたどり、見えた pid と group をまとめて止める。
        # shortcut: たどれなかった子孫は止められず、止まったかも確かめない。残ったプロセスが消えた場所へ書き込まないよう、一時ディレクトリは常に残して場所を知らせる。プロセスの取り残しが報告されたら止め方を見直す。
        kept.append(root)
        print(f"時間切れか中断で止めたので、一時ディレクトリを残す: {root}", file=sys.stderr)
        pids = [process.pid] + descendants(process.pid)
        groups = set()
        for pid in pids:
            try:
                groups.add(os.getpgid(pid))
            except OSError:
                pass
        groups.discard(os.getpgid(0))
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # 止めた後の回収にも上限を置き、ここで固まらないようにする。
        try:
            process.communicate(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
