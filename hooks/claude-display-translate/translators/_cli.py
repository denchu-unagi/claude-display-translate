import json
import re
import subprocess
import tempfile


class QuotaExhausted(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


class AuthFailed(RuntimeError):
    pass


# hook の _failure と同じ判定。translators は hook を import できない別プロセスで動くため複製する。
FAILURES = (
    (re.compile(r"session limit|usage limit|quota|insufficient credits"), QuotaExhausted),
    (re.compile(r"rate.?limit|too many requests|\b429\b"), RateLimited),
    (re.compile(r"unauthorized|unauthorised|authentication|invalid api key|\b401\b"), AuthFailed),
)


def run_cli(argv, stdin_text, timeout, cwd=None, env=None):
    # 呼出し元と同じプロセスgroupで起動し、hookがgroupごと止めたときにCLIも止まるようにする。
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=cwd, env=env)
    try:
        stdout, stderr = process.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        # CLIだけを止め、CLIの子はhookがプロセスgroupごと止める。
        try:
            process.kill()
        except OSError:
            pass
        # killに失敗しても戻れるよう、回収の待ちにも上限を置く。
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise TimeoutError()
    if process.returncode != 0:
        # translate.log には例外の型名だけが残るので、原因を型で区別する。
        text = (stdout + stderr).decode("utf-8", "replace").lower()
        failure = next((kind for pattern, kind in FAILURES if pattern.search(text)), RuntimeError)
        raise failure("command failed: exit code %d" % process.returncode)
    return stdout.decode("utf-8", "replace")


def codex(prompt, timeout, model, effort):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md") as file:
        file.write(prompt)
        file.flush()
        argv = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "-m", model,
                "-c", 'model_reasoning_effort="%s"' % effort, "-c", "project_doc_max_bytes=0",
                "-c", 'web_search="disabled"', "-c", "model_instructions_file=" + json.dumps(file.name)]
        for feature in ("hooks", "plugins", "apps", "multi_agent", "shell_tool", "view_image",
                        "browser_use", "browser_use_external", "computer_use", "image_generation",
                        "code_mode_host", "code_mode", "skill_search", "memories"):
            argv += ["--disable", feature]
        argv += ["-"]
        output = run_cli(argv, "Translate the source.", timeout)
    text = ""
    for line in output.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text", "")
    return text
