import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from _cli import AuthFailed, RateLimited

SARASHINA_URL = "https://api.sarashina.ai/v1/translate"
SARASHINA_CHAT_URL = "https://api.sarashina.ai/v1/chat/completions"
SARASHINA_MAX_CHARS = 5000
SARASHINA_KEY_FILE = "~/.config/sarashina/api-key"
SARASHINA_REVIEW_MODEL = "sarashina3-mini"
# hook の INLINE_CODE_PATTERN・PLACEHOLDER と同じ規則。hook は同じ規則で inline code の欠落を検査する。
INLINE_CODE_PATTERN = r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)"
# hook の URL_PATTERN・URL_TRAILING_PUNCTUATION と同じ規則。hook は末尾の句読点を除いた URL が訳文にあるかを検査する。
URL_PATTERN = r"https?://[^\s<>()\[\]]+"
URL_TRAILING_PUNCTUATION = ".,;:"
PLACEHOLDER = "[[[CODE%d]]]"
PLACEHOLDER_PATTERN = r"\[\[\[CODE(\d+)\]\]\]"
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
    # 逆に URL の直後に空白なしで続く inline code は URL ごと伏せ、mini にも見せない。
    return re.sub(INLINE_CODE_PATTERN + "|" + URL_PATTERN, replace, text), codes, urls


def restore_pieces(text, pieces, missing_ok=False):
    # 総数だけ比べると、1つが消えて別の1つが重複した訳文を通してしまうため、番号ごとに1回あることを確かめる。
    for number, piece in pieces.items():
        placeholder = PLACEHOLDER % number
        count = text.count(placeholder)
        # 翻訳APIは括弧の中が placeholder だけの部分を括弧ごと落とすことがある。落ちた inline code は照合で原文から補わせる。
        if count == 0 and missing_ok:
            continue
        if count != 1:
            raise RuntimeError("the translation dropped or repeated inline code or a URL")
        text = text.replace(placeholder, piece)
    return text


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


def call_within(budget, call):
    # urllib の timeout は接続と受信の操作ごとにかかり、合計では timeout を超えうるため、実時間で打ち切る。
    # 打ち切った呼び出しは止まらないが、daemon スレッドなので翻訳プログラムが結果を書いて終わるときに消える。
    done = {}

    def run():
        try:
            done["value"] = call()
        except BaseException as exc:
            done["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(max(budget, 0))
    if "error" in done and not isinstance(done["error"], TimeoutError):
        raise done["error"]
    return done.get("value")


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
    # 翻訳APIは指示を受け取れず、inline code の識別子まで訳すことがあるため、placeholder に置き換えて送る。
    protected, codes, urls = protect_code_and_urls(source.group(1))
    # APIが5000文字を超える入力を400で拒否するため、呼ばずに失敗させる。置き換えで長くなることがあるので、送る文字列で数える。
    if len(protected) > SARASHINA_MAX_CHARS:
        raise RuntimeError("source is longer than %d characters" % SARASHINA_MAX_CHARS)
    body = post_sarashina(SARASHINA_URL, key,
                          {"text": protected, "source_lang": "en", "target_lang": "ja"},
                          timeout)
    draft = trim_trailing_spaces(source.group(1), body["translated_text"])
    dropped = [piece for number, piece in codes.items() if PLACEHOLDER % number not in draft]
    text = restore_pieces(draft, codes, missing_ok=True)
    # URL を欠かす段が翻訳APIか sarashina3-mini か特定できていないため、URL は2回の見直しも placeholder のまま通し、最後に戻す。
    # 照合用の原文も同じ番号の placeholder にそろえる。
    mini_source = restore_pieces(protected, codes)
    # 翻訳APIの訳文に残る条件・状態の取り違えを、照合で減らすため。
    text = ask_mini(key, SARASHINA_REVIEW_PROMPT, mini_source, text, timeout - (time.monotonic() - started))
    # 照合だけでは翻訳APIの体言止めや直訳調が残り、sarashina3-mini単体より得点が下がったため、文体だけを整える。
    # 長文では文体の整えが時間内に終わらないことがあり、そのときは原文表示より読める照合済みの訳文を返す。
    # 照合の時間から間に合うかを予測すると、間に合う整えまで省くため、実際に時間切れになったときだけ省く。
    budget = timeout - (time.monotonic() - started)
    polished = call_within(budget, lambda: ask_mini(key, SARASHINA_POLISH_PROMPT, mini_source, text, budget))
    if polished is not None:
        text = polished
    # 存在だけを調べると、原文に同じ inline code が2回あって片方が落ち、照合でも補われない訳文を通してしまうため、出現回数まで比べる。
    if any(text.count(piece) < mini_source.count(piece) for piece in dropped):
        # 照合や整えでは欠落を補えないことがあるが、hook の翻訳指示と原文を渡して直接訳させると残るため、その経路に切り替える。
        budget = timeout - (time.monotonic() - started)
        # 残り時間が無いときに送ると urllib が負の timeout で失敗するため、送らずに答が無いものとして扱う。
        reply = None
        if budget > 0:
            reply = call_within(budget, lambda: post_sarashina(SARASHINA_CHAT_URL, key, {
                "model": SARASHINA_REVIEW_MODEL,
                "messages": [
                    {"role": "system", "content": prompt},
                    # "Translate the source." では、1行の短い英文を訳さずにそのまま返すことがあったため、訳す先の言語を書く。
                    {"role": "user", "content": "Translate the source into Japanese."},
                ],
            }, budget))
        # 元の失敗は時間切れではないので、hook の5分休止を起こす TimeoutError にはしない。
        content = reply["choices"][0]["message"]["content"] if reply is not None else ""
        if any(content.count(piece) < mini_source.count(piece) for piece in codes.values()):
            raise RuntimeError("the translation dropped inline code")
        # <translation> タグと URL の検査は hook が行い、URL はこの経路で placeholder 化していないので戻さない。
        return content
    return "<translation>\n" + restore_pieces(text, urls) + "\n</translation>"
