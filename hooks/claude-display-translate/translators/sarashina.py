import json
import os
import re
import time
import urllib.error
import urllib.request

SARASHINA_URL = "https://api.sarashina.ai/v1/translate"
SARASHINA_CHAT_URL = "https://api.sarashina.ai/v1/chat/completions"
SARASHINA_MAX_CHARS = 5000
SARASHINA_KEY_FILE = "~/.config/sarashina/api-key"
SARASHINA_REVIEW_MODEL = "sarashina3-mini"
SARASHINA_REVIEW_PROMPT = (
    "あなたは英日翻訳の校閲者です。<source> の英文と <draft> の日本語訳を比べ、"
    "意味が変わっている箇所（否定・条件・数値・主語・状態や完了の有無・合否などの取り違え、抜け、付け足し）だけを直してください。"
    "それ以外の言い回しは変えないでください。"
    "Markdown の構造、[[[CODEn]]] の形の文字列、インラインコード、URL、ファイルパス、コマンド名、識別子、すでに日本語の部分はそのまま残してください。"
    "直した訳文の全体を <translation> と </translation> で囲んで返し、その前後には何も書かないでください。"
)
SARASHINA_POLISH_PROMPT = (
    "あなたは日本語の校正者です。<source> の英文の日本語訳 <draft> を、意味を変えずに日本語として自然な文に整えてください。"
    "直すのは、文末の体言止め、直訳調の言い回し、主語と述語がかみ合わない文だけです。それ以外の言い回しは変えないでください。"
    "Markdown の構造、[[[CODEn]]] の形の文字列、インラインコード、URL、ファイルパス、コマンド名、識別子、すでに日本語の部分はそのまま残してください。"
    "整えた訳文の全体を <translation> と </translation> で囲んで返し、その前後には何も書かないでください。"
)


def post_sarashina(url, key, body, timeout):
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("sarashina request failed: HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        # 接続段階の時間切れはURLErrorに包まれて届くので、cooldownのためTimeoutErrorにそろえる。
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError() from None
        raise RuntimeError("sarashina request failed: %s" % type(exc.reason).__name__)


def trim_trailing_spaces(source, text):
    # 翻訳APIは原文に無い行末の空白2つ（Markdownの強制改行）を付けることがあるため、原文に強制改行が無いときは落とす。
    if any(line.endswith("  ") for line in source.split("\n")):
        return text
    return "\n".join(line.rstrip(" \t") for line in text.split("\n"))


def ask_mini(key, instructions, source, draft, timeout):
    if timeout <= 0:
        raise TimeoutError()
    reply = post_sarashina(SARASHINA_CHAT_URL, key, {
        "model": SARASHINA_REVIEW_MODEL,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": "<source>\n" + source + "\n</source>\n<draft>\n" + draft + "\n</draft>"},
        ],
        # 既定の0.7では、同じ英文でも回によって直し漏れや余計な言い換えが出たため。
        "temperature": 0.1,
    }, timeout)
    content = reply["choices"][0]["message"]["content"]
    tagged = re.fullmatch(r"\s*<translation>\n?(.*?)\n?</translation>\s*", content, re.S)
    if tagged is None:
        raise RuntimeError("sarashina3-mini reply is not tagged")
    return tagged.group(1)


def translate(prompt, timeout, settings):
    started = time.monotonic()
    path = getattr(settings, "SARASHINA_API_KEY_FILE", SARASHINA_KEY_FILE)
    with open(os.path.expanduser(path), encoding="utf-8") as key_file:
        key = key_file.read().strip()
    if not key:
        raise RuntimeError("the API key file is empty")
    # このAPIは指示を受け取れないため、原文だけを送る。
    source = re.search(r"<source>\n(.*)\n</source>", prompt, re.S)
    if source is None:
        raise RuntimeError("source not found")
    # 無償ベータでは送った文章が学習に使われることがあるため、キーを含む原文は送らない。
    if key in source.group(1):
        raise RuntimeError("source contains the API key")
    # APIが5000文字を超える入力を400で拒否するため、呼ばずに失敗させる。
    if len(source.group(1)) > SARASHINA_MAX_CHARS:
        raise RuntimeError("source is longer than %d characters" % SARASHINA_MAX_CHARS)
    body = post_sarashina(SARASHINA_URL, key,
                          {"text": source.group(1), "source_lang": "en", "target_lang": "ja"},
                          timeout)
    text = trim_trailing_spaces(source.group(1), body["translated_text"])
    # 翻訳APIの訳文に残る条件・状態の取り違えを、照合で減らすため。
    text = ask_mini(key, SARASHINA_REVIEW_PROMPT, source.group(1), text, timeout - (time.monotonic() - started))
    # 照合だけでは翻訳APIの体言止めや直訳調が残り、sarashina3-mini単体より得点が下がったため、文体だけを整える。
    text = ask_mini(key, SARASHINA_POLISH_PROMPT, source.group(1), text, timeout - (time.monotonic() - started))
    return "<translation>\n" + text + "\n</translation>"
