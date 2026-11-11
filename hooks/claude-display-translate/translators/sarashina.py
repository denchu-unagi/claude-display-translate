import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request

from _cli import AuthFailed, RateLimited

SARASHINA_URL = "https://api.sarashina.ai/v1/translate"
SARASHINA_MAX_CHARS = 5000
SARASHINA_KEY_FILE = "~/.config/sarashina/api-key"
# 翻訳APIは5000文字を超える入力を拒否するため、それに近い長さの原文は空行で2部分に分けて並列に訳す。
MIN_SPLIT_CHARS = 4800
# hook の INLINE_CODE_PATTERN・PLACEHOLDER と同じ規則。hook は同じ規則で inline code の欠落を検査する。
INLINE_CODE_PATTERN = r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)"
# hook の URL_PATTERN・URL_TRAILING_PUNCTUATION と同じ規則。hook は末尾の句読点を除いた URL が訳文にあるかを検査する。
URL_PATTERN = r"https?://[^\s<>()\[\]]+"
URL_TRAILING_PUNCTUATION = ".,;:"
PLACEHOLDER = "[[[CODE%d]]]"
PLACEHOLDER_PATTERN = r"\[\[\[CODE(\d+)\]\]\]"
TOKEN = "XQZ%dQ"
TOKEN_PATTERN = r"XQZ(\d+)Q"
# hook の structure と同じ数え方（段落・箇条書き・見出しの数）。translator は hook を import できない別プロセスで動くため複製する。
LIST_ITEM_PATTERN = r"[ \t]*(?:[-*+]|\d+[.)])[ \t]"
HEADING_PATTERN = r"#{1,6}[ \t]"


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
        # translate.log には例外の型名だけが残るので、キーの誤りと回数制限を型で区別する。
        failure = {401: AuthFailed, 429: RateLimited}.get(exc.code, RuntimeError)
        raise failure("sarashina request failed: HTTP %s" % exc.code)
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


def protect_code_and_urls(text):
    # 原文にある placeholder（hook が伏せたコードブロックなど）より大きい番号を使い、衝突させない。
    start = max(map(int, re.findall(PLACEHOLDER_PATTERN, text)), default=-1) + 1
    codes, urls = {}, {}

    def replace(match):
        number = start + len(codes) + len(urls)
        if match.group(1):
            codes[number] = match.group(0)
            return PLACEHOLDER % number
        # 文末の句読点は訳させる。hook も URL 末尾の句読点を除いて検査する。
        url = match.group(0).rstrip(URL_TRAILING_PUNCTUATION)
        urls[number] = url
        return PLACEHOLDER % number + match.group(0)[len(url):]

    # inline code を先に照合し、バッククォートの中の URL は inline code として扱う。URL_PATTERN は閉じのバッククォートまで取り込むため。
    # 逆に URL の直後に空白なしで続く inline code は URL ごと伏せ、翻訳APIにも見せない。
    return re.sub(INLINE_CODE_PATTERN + "|" + URL_PATTERN, replace, text), codes, urls


def restore_pieces(text, pieces):
    # 総数だけ比べると、1つが消えて別の1つが重複した訳文を通してしまうため、番号ごとに1回あることを確かめる。
    for number, piece in pieces.items():
        placeholder = PLACEHOLDER % number
        if text.count(placeholder) != 1:
            raise RuntimeError("the translation dropped or repeated inline code or a URL")
        text = text.replace(placeholder, piece)
    return text


def structure(text):
    all_lines = text.split("\n")
    paragraphs = sum(1 for i, line in enumerate(all_lines) if line.strip() and (i == 0 or not all_lines[i - 1].strip()))
    lines = [line for line in all_lines if line.strip()]
    return (paragraphs, sum(1 for line in lines if re.match(LIST_ITEM_PATTERN, line)),
            sum(1 for line in lines if re.match(HEADING_PATTERN, line)))


def translate_api(key, text, timeout, style=None):
    body = {"text": text, "source_lang": "en", "target_lang": "ja"}
    if style is not None:
        body["style"] = style
    return post_sarashina(SARASHINA_URL, key, body, timeout)["translated_text"]


def translate_api_tokens(key, text, timeout, style=None):
    # 翻訳APIは placeholder を括弧ごと落とすことがあり、試した62個のうち [[[CODEn]]] の形は56個、XQZnQ の形は61個が訳文に残ったため、
    # 送る直前にこの形へ変え、受け取ったら戻す。
    # 原文に同じ形の文字列があると戻せないので、そのときは元の形のまま送る。
    if re.search(r"XQZ\d", text):
        return translate_api(key, text, timeout, style=style)
    sent = re.sub(PLACEHOLDER_PATTERN, lambda match: TOKEN % int(match.group(1)), text)
    out = translate_api(key, sent, timeout, style=style)
    return re.sub(TOKEN_PATTERN, lambda match: PLACEHOLDER % int(match.group(1)), out)


def split_source(text):
    """原文を空行の位置で2部分に分ける。各部分が5000文字の上限に収まりやすいよう、大きいほうの部分が最も小さくなる位置を選ぶ。"""
    if len(text) < MIN_SPLIT_CHARS:
        return [text]
    # 区切りの空行は左の部分の末尾に残し、つなぎ直すと原文に戻るようにする。
    cuts = [match.end() for match in re.finditer(r"\n\n", text) if match.end() < len(text)]
    if not cuts:
        return [text]
    cut = min(cuts, key=lambda end: max(end, len(text) - end))
    return [text[:cut], text[cut:]]


def translate_piece(key, source, timeout, started):
    # 翻訳APIは指示を受け取れず、inline code の識別子まで訳すことがあるため、inline code と URL は placeholder に置き換えて送る。
    protected, codes, urls = protect_code_and_urls(source)
    # APIが5000文字を超える入力を400で拒否するため、呼ばずに失敗させる。置き換えで長くなることがあるので、送る文字列で数える。
    if len(protected) > SARASHINA_MAX_CHARS:
        raise RuntimeError("source is longer than %d characters" % SARASHINA_MAX_CHARS)
    pieces = list(codes) + list(urls)

    def attempt(send, style=None):
        remaining = timeout - (time.monotonic() - started)
        # 残り時間が無いときに送ると urllib が負の timeout で失敗するため、送らずに時間切れとして扱う。
        if remaining <= 0:
            raise TimeoutError()
        draft = trim_trailing_spaces(source, send(key, protected, remaining, style=style))
        complete = (all(draft.count(PLACEHOLDER % number) == 1 for number in pieces)
                    and structure(draft) == structure(protected))
        return draft, complete

    draft, complete = attempt(translate_api_tokens)
    if not complete:
        # placeholder を落とす位置は XQZnQ と [[[CODEn]]] で別々だったため、2回目は元の形で訳す。
        # 翻訳APIは箇条書きの2項目を1つにまとめることがあり、style=plain ではそれが起きなかったため、2回目は plain にする。
        retry, complete = attempt(translate_api, style="plain")
        if complete:
            draft = retry
    return restore_pieces(restore_pieces(draft, codes), urls)


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
    source = source.group(1)
    parts = split_source(source)
    if len(parts) == 1:
        text = translate_piece(key, source, timeout, started)
    else:
        def translate_part(part):
            body = part.rstrip("\n")
            tail = part[len(body):]
            # 英字を含まない部分は訳すものがないので、送らずにそのまま残す。
            if not re.search(r"[A-Za-z]", re.sub(PLACEHOLDER_PATTERN, "", body)):
                return part
            return translate_piece(key, body, timeout, started) + tail

        with concurrent.futures.ThreadPoolExecutor(len(parts)) as pool:
            futures = [pool.submit(translate_part, part) for part in parts]
            # どれか1つでも失敗したら、その例外をそのまま伝える。スレッドは止められないので、伝えるのはもう片方が終わってから。
            text = "".join(future.result() for future in futures)
    return "<translation>\n" + text + "\n</translation>"
