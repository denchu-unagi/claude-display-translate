#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
import json
import os
import re
import sys
import time

STARTED = time.monotonic()
# ログ行から会話ログの該当メッセージを引けるよう、処理中のメッセージをログに記録する。
current_message = ""
MARK = "[自動翻訳]\n"
FAIL_MARK = "[翻訳失敗: 原文を表示]\n"
LONG_MARK = "[長文のため原文を表示]\n"
MOSTLY_JA_MARK = "[日本語が大半のため原文を表示]\n"
MAX_TRANSLATE_CHARS = 8000
RESERVED_TAGS = ("<source>", "</source>", "<translation>", "</translation>")
CHILD_TIMEOUT = 45
HOOK_BUDGET_SECONDS = 55
KILL_GRACE_SECONDS = 1
LOG_NAME = "translate.log"
LOG_MAX_BYTES = 1 << 20
LOG_VALUE_MAX_CHARS = 200
LOG_REPLY_HEAD_CHARS = 100
FAILURE_FLAG_NAME = "last-failure"
FAILURE_COOLDOWN_SECONDS = 300
FINAL_WAIT_SECONDS = 10.0
FINAL_POLL_SECONDS = 0.05
NONFINAL_WAIT_SECONDS = 0.5
NONFINAL_POLL_SECONDS = 0.02
LANG_READ_RETRIES = 10
RETRY_SECONDS = 0.02
GC_AGE_SECONDS = 3600
MIN_TRANSLATE_LETTERS = 20
MIN_BLOCK_KEEP_RATIO = 0.9
MAX_LENGTH_RATIO = 2
LENGTH_SLACK_CHARS = 100
LIST_ITEM_PATTERN = r"[ \t]*(?:[-*+]|\d+[.)])[ \t]"
HEADING_PATTERN = r"#{1,6}[ \t]"
# 日本語の行を伏せる際も行頭の字下げ・引用・リスト・見出しの記号は外に残し、訳文の構造を保つ。
LINE_PREFIX_PATTERN = r"[ \t]*(?:>[ \t]*)*(?:(?:[-*+]|\d+[.)])[ \t]+)?(?:#{1,6}[ \t]+)?"
# CommonMarkと同じく、同じ長さのバッククォート列同士だけを組にする。長さの違う列（行中の ``` と
# 後続の ` など）を組にすると、間の英文をコードとみなし、それを訳した正しい訳文を不忠実と判定する。
INLINE_CODE_PATTERN = r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)"
URL_PATTERN = r"https?://[^\s<>()\[\]]+"
MIN_JA_CHARS = 2
KANA_WEIGHT = 3
SYMBOL_PATTERN = r"[=;{}|<>$/\\]"
LETTER_PATTERN = "[A-Za-z]"
JA_CLASS = "[\u3040-\u30ff\u4e00-\u9fff]"
KANA_CLASS = "[\u3040-\u30ff]"
FENCE_MARKER = r"[ \t]*(`{3,}|~{3,})"
FENCE_PATTERN = r"(?ms)^([ \t]*)(`{3,}(?!`)|~{3,}(?!~))[^\n]*$.*?^[ \t]*\2[^\n]*$|^[ \t]*(?:`{3,}|~{3,})[^\n]*$.*\Z"
STATE_NAME_PATTERN = r"[A-Za-z0-9_-]+\.(lang|closed|lock|\d{5}\.(seen|held))(\.tmp)?"
PLACEHOLDER = "[[[CODE%d]]]"
PLACEHOLDER_PATTERN = r"\[\[\[CODE(\d+)\]\]\]"
PLACEHOLDER_BASE_STEP = 1000
PROMPT = "\n".join(
    [
        "You translate Markdown text from English into natural Japanese for a software engineer.",
        "The text to translate is at the end of this system prompt, between <source> and </source>. The user message only asks you to start and may carry unrelated context; never translate or repeat it.",
        "Return the translation wrapped in <translation> and </translation>, with nothing before or after.",
        "Preserve exactly: Markdown structure and list markers, [[[CODEn]]] placeholders, inline code, URLs, file paths, command names, identifiers, and any text already in Japanese.",
        "Translate faithfully: keep negations and their scope, do not add subjects such as 私は, do not add or drop information.",
    ]
)
CHILD_ARGS = [
    "-p",
    "--setting-sources", "",
    "--settings", '{"disableAllHooks":true,"disableClaudeAiConnectors":true,"autoMemoryEnabled":false}',
    "--strict-mcp-config", "--no-session-persistence",
    "--model", "claude-haiku-5-5", "--effort", "low",
    "--tools", "", "--disable-slash-commands",
    "--output-format", "text",
]


SECRET_PATTERN = re.compile(
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]+|"
    r"sk[-]ant[-][A-Za-z0-9_-]+|sk[-]proj[-][A-Za-z0-9_-]+|"
    r"glpat-[A-Za-z0-9_-]+|xox[abprs]-[A-Za-z0-9-]+|"
    r"\bBearer\s+[A-Za-z0-9._~-]{12,}|"
    r"\b(?:password|passwd|secret|token|api_?key)[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}",
    re.I,
)


def split_fences(text):
    lines = []
    fence = None
    for line in text.split("\n"):
        found = re.match(FENCE_MARKER, line)
        marker = found.group(1) if found else None
        if fence is None:
            if marker:
                fence = marker
            else:
                lines.append(line)
        elif marker and marker[0] == fence[0] and len(marker) >= len(fence):
            fence = None
    return lines, fence


def prose_text(text):
    lines = []
    for line in split_fences(text)[0]:
        if line.startswith(("    ", "\t")) or re.match(HEADING_PATTERN, line):
            continue
        lines.append(line)
    return body_tokens("\n".join(lines))


def count_chars(text):
    return count_tokens(prose_text(text))


def count_tokens(tokens):
    kana = japanese = english = words = 0
    for token in tokens:
        letters = len(re.findall(LETTER_PATTERN, token))
        if re.search(SYMBOL_PATTERN, token):
            if letters:
                continue
        else:
            english += letters
            words += 1 if letters else 0
        kana += len(re.findall(KANA_CLASS, token))
        japanese += len(re.findall(JA_CLASS, token))
    return kana, japanese, english, words


def split_line_prefix(line):
    matched = re.match(LINE_PREFIX_PATTERN, line)
    return line[:matched.end()], line[matched.end():]


def body_tokens(body):
    body = re.sub(INLINE_CODE_PATTERN, " ", body)
    body = re.sub(URL_PATTERN, " ", body)
    return body.split()


def hide_japanese_line(line):
    if not re.search(JA_CLASS, line):
        return False
    counts = count_tokens(body_tokens(split_line_prefix(line)[1]))
    return classify(counts) == "ja" or counts[2] == 0


def remaining_english(text):
    # 伏せた行とコードの placeholder は訳す本文ではないので、英字数に数えない。
    english = 0
    for line in text.split("\n"):
        body = re.sub(PLACEHOLDER_PATTERN, " ", split_line_prefix(line)[1])
        english += count_tokens(body_tokens(body))[2]
    return english


def classify(counts):
    kana, japanese, english, words = counts
    if kana >= 1 and japanese >= MIN_JA_CHARS and japanese >= words:
        return "ja"
    if english >= MIN_TRANSLATE_LETTERS:
        return "en"
    return None


def judge(text):
    return classify(count_chars(text))


def structure(text):
    all_lines = text.split("\n")
    paragraphs = sum(
        1 for i, line in enumerate(all_lines) if line.strip() and (i == 0 or not all_lines[i - 1].strip())
    )
    lines = [line for line in all_lines if line.strip()]
    items = sum(1 for line in lines if re.match(LIST_ITEM_PATTERN, line))
    headings = sum(1 for line in lines if re.match(HEADING_PATTERN, line))
    return paragraphs, items, headings


def missing_pieces(source, translated):
    pieces = []
    for match in re.finditer(INLINE_CODE_PATTERN, source):
        inner = match.group(0).strip("`")
        # 子CLIは `。` のように中身が日本語の記号だけのinline codeを「。」と括り直すことがある。
        # 中身にASCII（空白を含む）が無いものに限り、バッククォートを除いた中身が訳文にあれば欠落とみなさない。
        # そのため句読点だけのinline codeは、言及ごと訳文から消えても検出できない。
        pieces.append(inner if not any(c.isascii() for c in inner) else match.group(0))
    pieces += [url.rstrip(".,;:") for url in re.findall(URL_PATTERN, source)]
    return [piece for piece in pieces if piece not in translated]


def faithful(source, translated):
    source_paragraphs, source_items, source_headings = structure(source)
    paragraphs, items, headings = structure(translated)
    if paragraphs < source_paragraphs * MIN_BLOCK_KEEP_RATIO or items < source_items or headings < source_headings:
        return False
    return not missing_pieces(source, translated)


def make_key(session_id, message_id):
    raw = str(session_id)[:8] + "-" + str(message_id)
    return "".join(c if (c.isascii() and c.isalnum()) or c == "-" else "_" for c in raw)


def protect_code(text, hide_japanese=False):
    blocks = []
    hidden = []
    used = {int(n) for n in re.findall(PLACEHOLDER_PATTERN, text)}
    base = 0
    while any(n in used for n in range(base, base + PLACEHOLDER_BASE_STEP)):
        base += PLACEHOLDER_BASE_STEP

    def replace(match):
        if match.group(2) is None:
            return match.group(0)
        indent = match.group(1)
        blocks.append(match.group(0)[len(indent):])
        return indent + PLACEHOLDER % (base + len(blocks) - 1)

    def replace_tag(match):
        blocks.append(match.group(0))
        return PLACEHOLDER % (base + len(blocks) - 1)

    text = re.sub(FENCE_PATTERN, replace, text)
    # 日本語の行は予約タグより先に伏せる。後に伏せると行の本文にタグの placeholder が入り、戻せなくなる。
    if hide_japanese:
        lines = text.split("\n")
        for number, line in enumerate(lines):
            if not hide_japanese_line(line):
                continue
            prefix, body = split_line_prefix(line)
            blocks.append(body)
            hidden.append(len(blocks) - 1)
            lines[number] = prefix + PLACEHOLDER % (base + len(blocks) - 1)
        text = "\n".join(lines)
    # 予約タグは場所によらず出現ごとに隠す。原文の区切りと衝突させず、タグに触れた文も訳す。
    text = re.sub("|".join(map(re.escape, RESERVED_TAGS)), replace_tag, text)
    return text, blocks, base, hidden


def restore_code(text, blocks, base):
    for number in range(len(blocks)):
        if text.count(PLACEHOLDER % (base + number)) != 1:
            return None

    def restore(match):
        number = int(match.group(1)) - base
        return blocks[number] if 0 <= number < len(blocks) else match.group(0)

    return re.sub(PLACEHOLDER_PATTERN, restore, text)


def _failure(raw):
    text = raw.decode("utf-8", "replace").lower()
    if re.search(r"session limit|usage limit|quota|insufficient credits", text):
        return "quota_exhausted"
    if re.search(r"rate.?limit|too many requests|\b429\b", text):
        return "rate_limited"
    if re.search(r"unauthorized|unauthorised|authentication|invalid api key|\b401\b", text):
        return "auth_failed"
    return "cli_error"


def resolve_claude():
    override = os.environ.get("MSGDISP_CLAUDE_CMD")
    if override:
        try:
            command = json.loads(override)
        except ValueError:
            return None
        if isinstance(command, list) and command and all(isinstance(arg, str) for arg in command) and command[0]:
            return command
        return None
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
    if execpath and os.path.isfile(execpath):
        return [execpath]
    import shutil

    installed = shutil.which("claude")
    return [installed] if installed else None


def run_child(text):
    import signal
    import subprocess
    import tempfile

    if in_cooldown():
        return fail("skipped_cooldown")
    known_secrets = [os.environ[name] for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
                     if os.environ.get(name) and len(os.environ[name]) >= 8]
    if SECRET_PATTERN.search(text) or any(secret in text for secret in known_secrets):
        return fail("credential_in_input")
    command = resolve_claude()
    if not command:
        return fail("no_claude")
    # 端末アプリOrcaのagent hook連携と、親セッションのmessaging接続を子CLIへ引き継がない。
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ORCA_", "CLAUDE_CODE_MESSAGING_"))
           and k not in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE")}
    env["MSGDISP_TRANSLATE_CHILD"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    remaining = HOOK_BUDGET_SECONDS - (time.monotonic() - STARTED)
    try:
        timeout = min(float(os.environ.get("MSGDISP_CHILD_TIMEOUT") or CHILD_TIMEOUT),
                      remaining - KILL_GRACE_SECONDS)
    except ValueError:
        return fail("invalid_timeout")
    if not timeout > 0:
        return fail("budget")
    started = time.monotonic()
    # Claude Codeは子CLIのユーザーメッセージへメールアドレスや環境情報を自動で足す。原文を同じ
    # メッセージに入れるとHaikuがそれらを翻訳対象と取り違えるため、原文はsystem promptへ分ける。
    # argvは/procから他userにも読めるため、所有者だけが読める一時ファイルで渡す。
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md") as prompt:
        prompt.write(PROMPT + "\n\n" + text)
        prompt.flush()
        try:
            # timeout時に孫processも同じgroupで停止する。
            process = subprocess.Popen(command + CHILD_ARGS + ["--system-prompt-file", prompt.name],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, start_new_session=True)
        except Exception as exc:
            return fail("spawn_error", exception=type(exc).__name__)
        timed_out = False
        error = None
        try:
            stdout, stderr = process.communicate(b"Translate the source.", timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except Exception as exc:
            error = type(exc).__name__
        if timed_out or error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(
                    timeout=max(0.1, min(KILL_GRACE_SECONDS, HOOK_BUDGET_SECONDS - (time.monotonic() - STARTED))))
            except Exception:
                stdout, stderr = b"", b""
    elapsed = time.monotonic() - started
    # 短時間の失敗で後続の翻訳を止めないため、cooldownはtimeoutに限る。
    if timed_out:
        note_failure()
        return fail("timeout", elapsed)
    if error:
        return fail("cli_error", elapsed, exception=error)
    if process.returncode != 0:
        return fail(_failure(stdout + stderr), elapsed, exit_code=process.returncode)
    translated = stdout.decode("utf-8", "replace")
    if SECRET_PATTERN.search(translated) or any(secret in translated for secret in known_secrets):
        return fail("credential_in_output", elapsed)
    return translated


def translate(full, lead, hide_japanese=False):
    protected, blocks, base, hidden = protect_code(full, hide_japanese)
    started = time.monotonic()
    output = run_child("<source>\n" + protected + "\n</source>")
    elapsed = time.monotonic() - started
    if output is None:
        return None
    # 外枠を全体一致で取り出し、途中の閉じタグを訳文側に残して予約タグ検査で拒否する。
    tagged = re.fullmatch(r"\s*<translation>\n?(.*?)\n?</translation>\s*", output.replace("\r\n", "\n"), re.S)
    if tagged is None:
        return fail("untagged", elapsed)
    text = tagged.group(1)
    wrapped = re.fullmatch(r"\s*```[^\n]*\n(.*)\n```\s*", text, re.S)
    if wrapped and not re.search(r"(?m)^```", wrapped.group(1)):
        text = wrapped.group(1) + "\n"
    if not re.search(KANA_CLASS, text):
        # 仮名の無い返答は再現しにくいため、原因を後から確かめられるよう先頭だけ記録する。
        return fail("no_japanese", elapsed, reply_head=text[:LOG_REPLY_HEAD_CHARS])
    # 子CLIが足した文脈を訳した出力は忠実性検査を通ることがあるため、訳文として表示しない。
    # 予約タグは保護時にplaceholderへ移したので、元に戻す前の訳文にあれば足された文脈を含む。
    # 長さは原文の2倍に余裕を足した閾値で検査し、余裕は数文字の原文の正訳を通すために置く。
    if (any(tag in text for tag in RESERVED_TAGS)
            or text.count("<system-reminder>") > protected.count("<system-reminder>")
            or len(text) > len(protected) * MAX_LENGTH_RATIO + LENGTH_SLACK_CHARS):
        return fail("echoed_context", elapsed)
    # 子CLIは `5.（保留）` のように番号直後の空白を落とすことがあり、CommonMarkではリスト項目にならない。
    # 全角数字の番号はCommonMarkでもリスト項目にならないので、半角数字の番号だけを補う。
    text = re.sub(r"(?m)^([ \t]*[0-9]+[.)])(?=[^\x00-\x7f])", r"\1 ", text)
    if not faithful(protected, text):
        counts = ["%d/%d" % pair for pair in zip(structure(protected), structure(text))]
        return fail("unfaithful", elapsed, blocks=counts[0], items=counts[1], heads=counts[2],
                    missing="".join(missing_pieces(protected, text)[:1]))
    if hide_japanese:
        # 各 placeholder が1回ずつ現れても入れ替わると日本語の行の順序が変わるため、出現順も比べる。
        # 予約タグの placeholder は訳文の語順で動いてよいので、伏せた行の placeholder だけを比べる。
        if ([int(n) for n in re.findall(PLACEHOLDER_PATTERN, text) if int(n) - base in hidden]
                != [base + number for number in hidden]
                or not all(re.search(r"(?m)^[ \t>#*+0-9.)-]*" + re.escape(PLACEHOLDER % (base + number)) + r"[ \t]*$", text)
                           for number in hidden)):
            return fail("placeholder", elapsed)
    restored = restore_code(text, blocks, base)
    if restored is None:
        return fail("placeholder", elapsed)
    if lead:
        matched = re.match(r"\s*" + re.escape(lead), restored)
        if matched is None:
            return fail("lead", elapsed)
        restored = restored[matched.end():]
    else:
        restored = restored.lstrip("\n")
    log_event("translated", elapsed, translated_chars=len(text))
    return restored


def state_dir():
    return os.environ.get("MSGDISP_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "state", "msgdisp"
    )


def ensure_state_dir():
    # 状態ファイルには表示を保留した本文が入るので、所有者だけが読めるディレクトリに置く。
    # makedirsのmodeは既存のディレクトリに効かないため、毎回権限を揃える。
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    os.chmod(state_dir(), 0o700)


def state_path(key, suffix):
    return os.path.join(state_dir(), key + "." + suffix)


def log_value(value):
    text = str(value)[:LOG_VALUE_MAX_CHARS]
    # 空白・引用符・等号を含む値と空の値はJSON文字列で囲み、1行をkey=valueの並びとして読めるようにする。
    return json.dumps(text, ensure_ascii=False) if not text or re.search(r'[\s"=\\]', text) else text


def log_event(result, elapsed=None, **fields):
    try:
        ensure_state_dir()
        path = os.path.join(state_dir(), LOG_NAME)
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        pairs = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "result": result}
        if current_message:
            pairs["message"] = current_message
        if elapsed is not None:
            pairs["elapsed_sec"] = "%.1f" % elapsed
        pairs.update(fields)
        line = " ".join("%s=%s" % (key, log_value(value)) for key, value in pairs.items()) + "\n"
        with open(path, "ab") as handle:
            handle.write(line.encode("utf-8"))
    except OSError:
        pass


def describe_error(exc):
    return "%s: %s" % (type(exc).__name__, exc)


def fail(reason, elapsed=None, **fields):
    log_event(reason, elapsed, **fields)
    return None


def note_failure():
    try:
        flag = os.path.join(state_dir(), FAILURE_FLAG_NAME)
        open(flag, "ab").close()
        os.utime(flag, None)
    except OSError:
        pass


def in_cooldown():
    try:
        flag = os.path.join(state_dir(), FAILURE_FLAG_NAME)
        return time.time() - os.path.getmtime(flag) < FAILURE_COOLDOWN_SECONDS
    except OSError:
        return False


def batch_path(key, index, kind):
    return state_path(key, "%05d.%s" % (index, kind))


def read_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def write_state(path, text):
    ensure_state_dir()
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(text.encode("utf-8"))
    os.replace(temporary, path)


def parse_lang(text):
    if text == "ja":
        return ("ja", 0)
    if text.startswith("en ") and text[3:].isascii() and text[3:].isdigit():
        return ("en", int(text[3:]))
    return None


def read_lang(key):
    path = state_path(key, "lang")
    for attempt in range(LANG_READ_RETRIES + 1):
        try:
            text = read_text(path)
        except FileNotFoundError:
            return None
        except OSError:
            text = ""
        parsed = parse_lang(text)
        if parsed is not None:
            return parsed
        if attempt < LANG_READ_RETRIES:
            time.sleep(RETRY_SECONDS)
    return None


def create_lang(key, value):
    ensure_state_dir()
    try:
        descriptor = os.open(state_path(key, "lang"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value.encode("utf-8"))
    return True


def collect(key, kind):
    found = {}
    prefix = key + "."
    suffix = "." + kind
    directory = state_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        middle = name[len(prefix):-len(suffix)]
        if not (middle.isascii() and middle.isdigit()):
            continue
        try:
            found[int(middle)] = read_text(os.path.join(directory, name))
        except OSError:
            pass
    return found


def locked(key):
    import fcntl

    # flushごとのhookは並行に起動される。非finalのclosed確認とheld書込を、finalのclosed作成と
    # 排他にしないと、finalが回収したheldを非finalが原文でも表示し、同じ行が二重に出る。
    ensure_state_dir()
    handle = open(state_path(key, "lock"), "ab")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def joined(parts, below=None):
    return "".join(parts[i] for i in sorted(parts) if below is None or i < below)


def cleanup(key):
    directory = state_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    now = time.time()
    prefix = key + "."
    closed = key + ".closed"
    mtimes = {}
    for name in names:
        if re.fullmatch(STATE_NAME_PATTERN, name):
            try:
                mtimes[name] = os.path.getmtime(os.path.join(directory, name))
            except OSError:
                pass
    latest = {}
    for name, mtime in mtimes.items():
        owner = name.split(".", 1)[0]
        latest[owner] = max(latest.get(owner, 0), mtime)
    # 他messageの状態はowner単位で1時間更新がないときだけ消す。未完了のmessageの保留行を消さないため。
    for name in mtimes:
        owner = name.split(".", 1)[0]
        if (name.startswith(prefix) and name != closed) or now - latest[owner] > GC_AGE_SECONDS:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass


def emit(content):
    payload = {
        "hookSpecificOutput": {"hookEventName": "MessageDisplay", "displayContent": content}
    }
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def handle_non_final(key, index, delta):
    if os.path.exists(state_path(key, "closed")):
        log_event("late", index=index)
        return
    lang = read_lang(key)
    if lang is None and index > 0:
        wait_for_batches(key, index, NONFINAL_WAIT_SECONDS, NONFINAL_POLL_SECONDS, True)
        lang = read_lang(key)
    if lang is None:
        verdict = judge(joined(collect(key, "seen"), index) + delta)
        if verdict == "en" and in_cooldown():
            verdict = None
        if verdict is not None:
            value = "ja" if verdict == "ja" else "en %d" % index
            lang = parse_lang(value) if create_lang(key, value) else read_lang(key)
        if lang is None:
            write_state(batch_path(key, index, "seen"), delta)
            return
    if lang[0] == "ja":
        return
    with locked(key):
        if os.path.exists(state_path(key, "closed")):
            log_event("late", index=index)
            return
        write_state(batch_path(key, index, "held"), delta)
    emit(MARK if index == lang[1] else "")


def wait_for_batches(key, index, limit=FINAL_WAIT_SECONDS, poll=FINAL_POLL_SECONDS, any_lang=False):
    deadline = time.monotonic() + limit
    while True:
        lang = read_lang(key)
        if lang is not None and (any_lang or lang[0] == "ja"):
            return
        present = set(collect(key, "seen")) | set(collect(key, "held"))
        if all(i in present for i in range(index)):
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(poll)


def final_output(key, index, delta, state):
    wait_for_batches(key, index)
    try:
        with locked(key):
            write_state(state_path(key, "closed"), "")
    except OSError:
        pass
    lang = read_lang(key)
    if lang is not None and lang[0] == "ja":
        return None
    if lang is not None:
        state["en"] = True
    seen = collect(key, "seen")
    if lang is not None:
        # seenは言語未確定の非finalが原文で表示済みの行なので、訳文に含めると二重に出る。
        state["full"] = joined(collect(key, "held")) + delta
        before = joined(seen, lang[1])
    else:
        if in_cooldown() or judge(joined(seen) + delta) != "en":
            return None
        state["prefix"] = MARK
        state["full"] = delta
        before = joined(seen)
    full = state["full"]
    lead = split_fences(before)[1]
    lead = lead + "\n" if lead else ""
    source = lead + full
    counts = count_chars(source)
    hide_japanese = False
    if classify(counts) == "ja" and counts[0] * KANA_WEIGHT >= counts[2]:
        if state["prefix"]:
            log_event("skipped_mostly_ja", ja_chars=counts[1], en_words=counts[3])
            return full
        # 言語確定済みなら非finalが [自動翻訳] を表示済みで取り消せない。日本語の行は言い換えを避けるため
        # 翻訳に送らず英語の行だけを訳し、訳す英語がほとんど残らないときは訳していないことを印で示す。
        protected = protect_code(source, True)[0]
        if remaining_english(protected) < MIN_TRANSLATE_LETTERS:
            log_event("skipped_mostly_ja", ja_chars=counts[1], en_words=counts[3])
            return MOSTLY_JA_MARK + full
        hide_japanese = True
        length = len(protected)
    else:
        length = len(protect_code(source)[0])
    if not hide_japanese and counts[2] == 0:
        return full
    if length > MAX_TRANSLATE_CHARS:
        log_event("skipped_long", source_chars=length)
        return None if state["prefix"] else LONG_MARK + full
    translated = translate(source, lead, hide_japanese)
    if translated is None:
        return FAIL_MARK + full
    return state["prefix"] + translated


def handle_final(key, index, delta):
    state = {"full": None, "prefix": "", "en": False}
    output = None
    try:
        try:
            output = final_output(key, index, delta, state)
        except Exception as exc:
            log_event("error", error=describe_error(exc))
            if state["full"] is not None:
                output = FAIL_MARK + state["full"]
            elif state["en"]:
                output = FAIL_MARK + joined(collect(key, "held")) + delta
        if output is not None:
            emit(output)
    finally:
        cleanup(key)


def main():
    global current_message
    raw = sys.stdin.buffer.read()
    if os.environ.get("MSGDISP_TRANSLATE_CHILD"):
        return
    if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") != "1" and not os.environ.get(
        "MSGDISP_FORCE_ATTENDED"
    ):
        return
    try:
        data = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("hook_event_name") != "MessageDisplay"
            or not data.get("message_id")
        ):
            return
        index = data.get("index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            index = 0
        final = data.get("final", False) is True
        delta = data.get("delta", "")
        if not isinstance(delta, str):
            delta = ""
        session_id = data.get("session_id") or ""
        key = make_key(session_id, data["message_id"])
        current_message = key
        if final:
            handle_final(key, index, delta)
        else:
            handle_non_final(key, index, delta)
    except Exception as exc:
        log_event("error", error=describe_error(exc))


if __name__ == "__main__":
    main()
