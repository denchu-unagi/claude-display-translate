# Sarashina で翻訳する

[Sarashina](https://sarashina.ai/) は、ソフトバンク傘下の SB Intuitions が開発する国産の LLM です。2026年11月時点では無償ベータ版として提供されていて、API も無料で使えます。

README の[翻訳モデルを変える](../README.md#翻訳モデルを変える)で `MODEL = "sarashina"` にすると、Sarashina で翻訳します。選ぶ前に、次の[注意](#sarashina-を使うときの注意)を読んでください。

## Sarashina を使うときの注意

[Sarashinaサービス利用規約](https://sarashina.ai/terms)のうち、この hook での使い方に関わる点です。

- 第3条により、利用できるのは日本国内に住む個人か、日本国内に本店がある法人だけです。
- 第10条により、個人を特定できる情報や第三者の著作物などは入力できません。この hook は英語の返答を自動で送るので、返答に人名やメールアドレスなどが含まれていても、閉じたコードブロックの外にあればそのまま送ります。
- 第9条と第11条により、送った文章と出力は、無償・有償を問わずモデルの学習に使われることがあります。学習への利用を拒否できるのは有償プランだけです。
- 閉じたコードブロックを除き、inline code と URL をプレースホルダーに置き換えた状態で5000文字を超える原文は、Sarashina API が受け付けないため、`[翻訳失敗: 原文を表示]` を付けて原文を表示します。

## 訳し方

`MODEL = "sarashina"` にすると、`sarashina3-translate` の訳文を `sarashina3-mini` が2回見直します。

1. 原文と照らし合わせて、意味の誤りだけを直します。`sarashina3-translate` だけでは誤訳が残り、下の比較で不合格だったためです。
2. 文末の体言止め、直訳調の言い回し、主語と述語がかみ合わない文だけを直します。1だけでは下訳の文体が残り、`sarashina3-mini` 単体より得点が低かったためです。

2の見直しが時間内に終わらないときは、1で直した訳文を表示します。長文では3段の合計が時間切れになることがあり、原文を表示するより読めるためです。1だけの訳文の品質は、下の比較では測っていません。

`sarashina3-translate` には指示を渡せず、inline code の中の識別子まで日本語に訳すことがあります。そのため inline code はプレースホルダーに置き換えて `sarashina3-translate` へ送り、訳文で元に戻します。`sarashina3-translate` は括弧の中がプレースホルダーだけの部分を括弧ごと落とすことがあります。落ちた inline code は `sarashina3-mini` の見直しで原文から補わせます。見直しでも補われないことがあるため、補われなかったとき（原文より出現回数が少ないとき）は、`sarashina3-mini` に hook の翻訳指示をそのまま渡して原文から直接訳させ、それでも欠けたときは原文を表示します。このとき、訳し始めの指示は `Translate the source into Japanese.` にしています。下の比較の `sarashina3-mini` と同じ `Translate the source.` では、1行の短い英文を訳さずにそのまま返すことがあったためです。下の比較の得点は `Translate the source.` で測った値です。

URL もプレースホルダーに置き換えて送ります。URL は `sarashina3-mini` の2回の見直しにもプレースホルダーのまま渡し、最後に元へ戻します。訳文から URL が欠けたとき、欠かしたのが `sarashina3-translate` か `sarashina3-mini` かを特定できていないためです。

## 比較

README の[翻訳モデルの比較](../README.md#翻訳モデルの比較)と同じ英文と採点で比べた結果です。`claude-haiku-5-5` や `gpt-6-luna` などの結果は、README のその表にあります。Sarashina のモデルの時間は、API を直接呼んだ時間です。

| モデル | CLI | effort | 翻訳の中央値 | 得点（5段階） | 重大な誤訳 | 必須事実の欠落 | 判定 |
|---|---|---|---|---|---|---|---|
| `sarashina3-mini` | Sarashina API | — | 1.34秒 | 4.44 | 0件 | 0件 | 合格 |
| `sarashina3-translate` + `sarashina3-mini` | Sarashina API | — | 4.15秒 | 4.58 | 0件 | 0件 | 合格 |
| `sarashina3-translate` | Sarashina API | — | 1.27秒 | 3.67 | 3件 | 0件 | 不合格 |
| `sarashina3.2-mini-thinking` | Sarashina API | low | 10.62秒 | 4.31 | 1件 | 0件 | 不合格 |

- `sarashina3-translate` + `sarashina3-mini` は `sarashina` で測りました。`sarashina3-mini` の2回の見直しは temperature 0.1 で動かします。API の既定値の 0.7 では、同じ英文でも回によって直し漏れや余計な言い換えが出たためです。
- `sarashina3-mini` と `sarashina3.2-mini-thinking` は、hook の翻訳指示をそのまま渡し、temperature などを API の既定値のまま測った参考値です。`sarashina3.2-mini-thinking` は effort を low にしました。この2つは `MODEL` では選べません。

## Sarashina の API キー

1. [API コンソール](https://chat.sarashina.ai/dashboard/api-keys)でキーを発行します。
2. Claude Code の外の端末で次を実行し、表示されたプロンプトにキーを貼り付けます。入力は画面に表示されず、自分だけが読めるファイル `~/.config/sarashina/api-key` に保存されます。

   ```bash
   ( umask 077; mkdir -p ~/.config/sarashina; printf 'Sarashina API key: '; read -rs key; echo; printf '%s\n' "$key" > ~/.config/sarashina/api-key )
   ```

- キーを Claude Code に貼り付けたり、このファイルを Claude Code に読ませたりしないでください。Claude Code が読んだキーは Anthropic に送られます。さらに `haiku` や `luna` に切り替えているときに Claude の返答にキーが含まれると、この hook がその提供元へも送ります。
- キーを別のファイルに置くときは、`settings.py` に `SARASHINA_API_KEY_FILE = "<ファイルのパス>"` を書きます。
- 原文にキーの値が含まれるときは、Sarashina に送らずに `[翻訳失敗: 原文を表示]` を付けて原文を表示します。
