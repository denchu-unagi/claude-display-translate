# claude-display-translate

Claude Code が英語で返したメッセージを、画面表示だけ日本語に差し替える `MessageDisplay` hook です。

- 英語と判定したメッセージを `claude -p`（Claude Haiku 5.5、effort low）で日本語に訳して表示します。翻訳に使うモデルは[設定ファイル](#翻訳モデルを変える)で変えられます。
- 日本語のメッセージはそのまま表示します。ただし、日本語で始まるメッセージの途中に英語の行がまとまって現れたときは、その英語の行だけを訳して表示します。
- `` ``` `` や `~~~` で囲んだコードブロックは翻訳に渡さず、そのまま表示します。
- 文中の inline code と URL は訳さずに残すよう翻訳に指示し、訳文から欠けていたときは訳文の代わりに原文を表示します。

## 表示例

### hook なし

````text
The build failed because `npm ci` could not resolve a peer dependency.
Pin the version in `package.json` and run the install again:

```bash
npm install react@18.3.1 --save-exact
npm ci
```

See https://docs.npmjs.com/cli/v10/commands/npm-ci for details.
````

### hook あり

````text
[自動翻訳]
`npm ci` が peer dependency を解決できなかったため、ビルドが失敗しました。
`package.json` でバージョンを固定してから、もう一度インストールを実行してください:

```bash
npm install react@18.3.1 --save-exact
npm ci
```

詳細は https://docs.npmjs.com/cli/v10/commands/npm-ci を参照してください。
````

## メリット

### メインセッションのコンテキストを汚さない

`MessageDisplay` hook は画面表示だけを差し替えるため、訳文は会話履歴（transcript）にも Claude に送る内容にも入りません。
そのため、英語のメッセージが何件あっても、訳文のぶん会話履歴が増えることはありません。

[Claude Code の公式ドキュメント](https://code.claude.com/docs/ja/hooks)

Stop hook で訳文を Claude に返す方式では、訳文や翻訳の指示が会話に入り、そのぶんコンテキストを消費します。

また、翻訳は別に起動する `claude -p`（Claude Haiku 5.5）か、設定ファイルで選んだ翻訳先が行うので、メインセッションのモデルは翻訳に使われません。

- 英語の原文の代わりに訳文が表示され、原文と訳文が並んで画面が長くなりません。
- コマンドとコードは訳さないので、表示されたままコピーして使えます。

## 注意

- 英語のメッセージ1件ごとに翻訳の呼び出し（既定は Haiku）が1回増えます。サブスクリプションの利用枠または API 料金を消費します。
- 訳文は、メッセージの生成が終わってから表示されます。
- セッションを再開すると（`claude --resume`、`claude --continue`、`/resume`）、再開前に訳文で表示していたメッセージは、英語の原文で表示されます。会話履歴（transcript）には訳文ではなく原文が保存されます。
- 次の場合は原文を表示します。
  - 翻訳に失敗したときは `[翻訳失敗: 原文を表示]` を付けます。訳文に、原文に無いキリル文字やハングルなどの語が混ざったときも、翻訳の失敗として扱います。
  - 閉じたコードブロックを除いて8000文字を超えるときは `[長文のため原文を表示]` を付けます。
  - メッセージの最後で初めて英語と判定した長文には、印を付けません。
- 書き出しの英語を見て `[自動翻訳]` を表示した後、メッセージ全体はほぼ日本語だと分かったときは、英語の行だけを訳し、日本語の行は原文のまま表示します。英語が日本語の行の中にしかないなど、訳す英語がほとんど残らないときは、`[日本語が大半のため原文を表示]` を付けて原文を表示します。
- 日本語で始まるメッセージの途中に、英語の行を含む行のまとまりが来ると、その位置に `[自動翻訳]` を表示します。そこから後の表示はメッセージの終わりまで保留し、英語の行だけを訳します。日本語の行は原文のまま表示します。それより前の日本語はすぐに表示されます。まとまりの中の英語の行が短く、英字が合わせて20字未満のときは切り替えず、原文のまま表示します。`|` で始まる表の行は、英語の行として数えません。表示済みの表が空行で終わる前は、英語の行が続いても切り替えません。
- 翻訳が timeout した後の5分間は翻訳しません。
- API キーやトークンに見える文字列を含むメッセージは、翻訳に送りません。ただし閉じたコードブロック（`` ``` `` や `~~~`）の中は、検査も翻訳への送信もしません。閉じていないコードブロックやインデントで書いたコードは、本文と同じく検査して送ります。
- 対話セッションでだけ動きます。`claude -p` と Agent SDK では動きません。ただし、Agent SDK 経由で起動する Orca のチャット画面では動きます（[Orca のチャット画面で使う](#orca-のチャット画面で使う)）。
- 作業中に表示される Claude の思考（thinking）と、ツールの実行結果は翻訳しません。

## Orca のチャット画面で使う

- Orca のチャット画面でも翻訳します。チャット画面の `claude` は Agent SDK 経由で起動しますが、人が画面を見ているためです。
- Agent SDK 経由ではメッセージ全体が一度に届くため、全体で判定します。全体が英語と判定されないときは、全体に `[自動翻訳]` を付けて英語の行だけを訳します。英語と判定されたときは、英語のメッセージと同じく全体を訳します。
- チャット内の Bash ツールから起動した `claude -p` では翻訳しません。スクリプトが読む出力を訳文に変えないためです。
- `ps` が使えないなど、Bash ツールから起動した `claude -p` かどうかを判定できないときは、チャット画面でも翻訳しません。
- チャット内の Bash ツールから起動した `claude -p` でも、入れ子の `claude` を見分けられないとき（名前の違う `claude` を入れ子にしたときなど）は翻訳します。見分けられない条件と判定の方法は[開発者向けの説明](docs/development.md#orca-のチャット画面の判定)にあります。

## 翻訳にかかる時間

メッセージの生成が終わってから訳文が出るまでの目安です。作者の環境で約200件を実測しました（Claude Haiku 5.5、effort low）。

- 時間は翻訳用の `claude -p` の実行時間です。
- 文字数は訳文の長さです。閉じたコードブロックを短いプレースホルダーに置き換えた状態で数えています。

| 訳文の長さ | 件数 | 中央値 | 最大 |
|---|---|---|---|
| 300文字未満 | 194 | 2.0秒 | 5.2秒 |
| 300〜1000文字 | 5 | 4.6秒 | 5.6秒 |
| 1000〜2000文字 | 3 | 7.0秒 | 8.1秒 |

2000文字を超える訳文の実測値はありません。

## 翻訳モデルの比較

翻訳に使うモデルを選ぶために、同じ英文を CLI とモデルを変えて訳したベンチマーク結果です。
英文は次の6種類で、各3回、モデルごとに18件を訳しました。

- 短文
- 条件の多い指示
- Markdown
- コード
- 日本語混じり
- 約2000文字の長文

時間は CLI の起動を含む1件の翻訳時間の中央値です。

| モデル | CLI | effort | 翻訳の中央値 | 得点（5段階） | 重大な誤訳 | 必須事実の欠落 | 判定 |
|---|---|---|---|---|---|---|---|
| `claude-haiku-5-5` | Claude Code 2.1.301 | low | 3.14秒 | 4.92 | 0件 | 0件 | 合格（既定） |
| `gpt-6-luna` | Codex 0.163.0 | low | 4.83秒 | 4.75 | 0件 | 0件 | 合格 |
| `gpt-6.1-terra` | Codex 0.164.0 | low | 5.07秒 | 4.69 | 0件 | 0件 | 合格 |
| `claude-sonnet-5-5` | Claude Code 2.1.316 | low | 5.21秒 | 4.72 | 0件 | 0件 | 合格 |
| `deepseek-v4.1-flash` | pi 1.3.0（OpenCode Go） | low | 5.74秒 | 4.75 | 0件 | 0件 | 合格 |
| `gemini-3.8-flash` | Antigravity 1.4.12 | low | 8.89秒 | 4.83 | 0件 | 0件 | 合格 |

- 合格の条件は、18件すべてが45秒以内に終わって機械的な検査（日本語で返ること、Markdown・コード・表の書式が保たれることなど）に合格し、重大な誤訳と必須事実の欠落が0件で、得点の平均が4以上であることです。hook の既定は、追加の設定なしで使える `claude-haiku-5-5` にしています。
- 得点は、5が「自然で正確」、4が「意味は変わらないが小さな不自然さがある」です。訳したモデル名を伏せて Anthropic の `claude-opus-5-5` に2回採点させた平均で、誤訳の指摘は原文と照合して確かめました。

Sarashina のモデルの結果は、[Sarashina で翻訳する](docs/sarashina.md#比較)にあります。

## 動作要件

- Claude Code（2.1.301 で動作確認。`MessageDisplay` hook が必要）
- Python 3（標準ライブラリのみ。3.14 で動作確認）
- Linux、macOS、WSL。Windows ネイティブには対応しません（`fcntl` を使うため）。

## 導入手順

Claude Code の公式ドキュメントと同じ流れで設定ファイルに hook を追加します。Claude Code に頼んでも、手動でも、同じ4つの手順で導入できます。

[Claude Code の公式ドキュメント](https://code.claude.com/docs/ja/hooks-guide#set-up-your-first-hook)

1. **hook のファイルを置く**

   ディレクトリを作成し、スクリプトをダウンロードして実行権限を付与します。

   ```bash
   mkdir -p ~/.claude/hooks
   curl -fsSL https://raw.githubusercontent.com/denchu-unagi/claude-display-translate/main/hooks/claude-display-translate.py -o ~/.claude/hooks/claude-display-translate.py
   chmod +x ~/.claude/hooks/claude-display-translate.py
   ```

2. **`~/.claude/settings.json` に hook を追加する**

   `~/.claude/settings.json` を開き、次の `MessageDisplay` hook を追加します。ファイルが無ければ作成します。

   ```json
   {
     "hooks": {
       "MessageDisplay": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "\"$HOME/.claude/hooks/claude-display-translate.py\"",
               "timeout": 60
             }
           ]
         }
       ]
     }
   }
   ```

   すでに `hooks` があるときは、`hooks` 全体を置き換えずに、既存のイベントと並べて `MessageDisplay` を追加します。`MessageDisplay` もすでにあるときは、その配列に1件だけ追加します（同じ `command` の登録がすでにある場合は追加しません）。

   `timeout` を省くと Claude Code が10秒で hook を打ち切ってしまい、表示を保留していた英語の行が画面に出なくなります。翻訳には長いと数十秒かかり、hook 内部の上限も55秒のため、`timeout` には必ず `60` を指定してください。

   権限などの理由で Claude Code が `settings.json` を直接編集できなかった場合は、追加する内容を表示してユーザーに追記を依頼してください。

3. **`/hooks` で登録を確かめる**

   Claude Code で `/hooks` と入力すると、登録済みの hook の一覧が開きます。`MessageDisplay` の下にこの hook があれば登録できています。設定ファイルの変更は通常そのまま読み込まれます。一覧に出ないときは、Claude Code を起動し直してください。

4. **動作を確かめる**

   Claude に英語で答えるよう頼みます（例: 「英語で答えて: `git status` は何をするコマンド？」）。返答が `[自動翻訳]` 付きの日本語で表示されれば動いています。

## 止め方

`~/.claude/settings.json` の `hooks.MessageDisplay` から、この hook の登録を削除します。

## 設定

| 環境変数 | 用途 |
|---|---|
| `MSGDISP_STATE_DIR` | 状態とログの置き場所を変える。この hook 専用のディレクトリを指定する |
| `MSGDISP_CHILD_TIMEOUT` | 翻訳の timeout 秒（既定45秒、上限は hook 全体の予算55秒の残り） |
| `MSGDISP_CLAUDE_CMD` | Claude コマンドを JSON 配列で差し替える（テスト用。Haiku で翻訳するときだけ効く） |
| `MSGDISP_FORCE_ATTENDED` | 非対話セッションでも動かす |
| `MSGDISP_CONFIG_DIR` | 翻訳の設定のディレクトリを変える（既定は hook と同じディレクトリの `claude-display-translate`） |

## 翻訳モデルを変える

翻訳の設定ファイルを置くと、翻訳に使うモデルを変えられます。置かなければ、これまでどおり Haiku で翻訳します。

1. **設定ファイルとモデルのファイルを置く**

   hook と同じディレクトリの `claude-display-translate/` に、設定ファイルの見本と `translators/` のファイルを置きます。

   ```bash
   mkdir -p ~/.claude/hooks/claude-display-translate/translators
   base=https://raw.githubusercontent.com/denchu-unagi/claude-display-translate/main/hooks/claude-display-translate
   curl -fsSL "$base/settings.py.sample" -o ~/.claude/hooks/claude-display-translate/settings.py
   for f in _cli.py luna.py sarashina.py; do
     curl -fsSL "$base/translators/$f" -o ~/.claude/hooks/claude-display-translate/translators/$f
   done
   ```

2. **`MODEL` を書き換える**

   `~/.claude/hooks/claude-display-translate/settings.py` の `MODEL` を、次のどれかにします。

   | `MODEL` | 翻訳に使うもの | モデル | 必要なもの |
   |---|---|---|---|
   | `haiku` | hook に組み込みの `claude -p`。`settings.py` を置かないときと同じ | `claude-haiku-5-5`（effort low） | Claude Code |
   | `luna` | Codex CLI（`codex exec`） | `gpt-6-luna`（effort low） | Codex CLI へのログイン |
   | `sarashina` | Sarashina API | `sarashina3-translate`（翻訳 API だけ） | [Sarashina API のキー](docs/sarashina.md#sarashina-の-api-キー) |

   `sarashina` を選ぶ前に、[Sarashina を使うときの注意](docs/sarashina.md#sarashina-を使うときの注意)を読んでください。利用できる人や、送った文章の扱いに条件があります。

3. **次のメッセージから、書き換えた設定で翻訳する**

   設定ファイルは翻訳のたびに読み込むので、Claude Code の再起動は要りません。`settings.py` を削除すると、既定の Haiku での翻訳に戻ります。

- ほかのモデルを使うときは、`translators/` にモデルのファイルを置き、`MODEL` にそのファイル名から `.py` を除いた名前を書きます。ファイルの作り方は[開発者向けの説明](docs/development.md#翻訳モデルのファイルを作る)にあります。
- `MODEL` の名前が不正なときや、`translators/` にそのファイルが無いときは、`[翻訳失敗: 原文を表示]` を付けて原文を表示し、`translate.log` に `translator_error` を記録します。モデルのファイルで起きた例外（CLI の異常終了を含む）も同じく `translator_error` として、例外の型名だけを記録します。同梱の `luna` は、利用枠切れ・回数制限・認証の失敗を、それぞれ `QuotaExhausted`・`RateLimited`・`AuthFailed` という型名で記録します。`sarashina` は、HTTP 429 を `RateLimited`、401 を `AuthFailed` として記録します。`haiku` の失敗は、`settings.py` を置かないときと同じく、利用枠切れなどの原因ごとに記録します。
- 翻訳する文は、選んだモデルの提供元へ送られます。
- [注意](#注意)に書いた翻訳に送る前の検査、timeout 後に翻訳を休む動作、訳文の検査は、どのモデルでも hook が行います。
- 設定ファイルとモデルのファイルは Python の隔離モード（`python3 -I`）で実行します。`PYTHONPATH` などの `PYTHON` で始まる環境変数、作業ディレクトリにあるモジュール、ユーザーの site-packages（`pip install --user` で入れたもの）は読み込みません。書き換えた内容がすぐに使われるよう、Python のキャッシュ（`.pyc`）は作らず、既にあるキャッシュも読みません。

## 状態とログ

状態は `~/.claude/hooks/state/msgdisp/` に置きます。所有者だけがアクセスできる権限（700）にします。

- 言語を判定するまでの行と、表示を保留している行の本文を、メッセージごとにここへ一時保存します。メッセージの最後で削除します。
- Claude Code が途中で終了するなどして残った分は、別のメッセージの最後に、最後の更新から1時間たったものを削除します。
- Haiku で翻訳する間だけ、原文を OS の一時ディレクトリに所有者だけが読めるファイルとして書き、翻訳が終わると削除します。
- 翻訳の設定ファイルを使うときは、翻訳のたびに hook が一時ディレクトリを用意して `TMPDIR` として渡し、翻訳が終わると中身ごと削除します。
- `translate.log` には1件の結果を1行の `key=value` で記録します。形式と記録する項目は[開発者向けの説明](docs/development.md#ログの形式)にあります。

  本文は記録しません。ただし訳文の検証に失敗したとき（`unfaithful`）は原文の inline code か URL を1つ、訳文の仮名が翻訳コマンドに送った原文より増えなかったとき（`no_japanese`）は返答の先頭100文字を `reply_head` に記録します。

## 仕組み

1. ストリーミング中のメッセージを英語と判定した時点で、以降の行の表示を保留して `[自動翻訳]` を表示します。
2. メッセージの最後で、保留した全文のうち閉じたコードブロックと区切り用のタグをプレースホルダーに置き換え、system prompt として `claude -p` に渡します。設定ファイルで `haiku` 以外のモデルを選んだときは、`claude -p` の代わりに、そのモデルのファイルの `translate()` に渡します。
3. 訳文で段落・リスト・見出しが大きく欠けていないこと、原文の inline code と URL が訳文に含まれていることを確かめてから表示します。

翻訳を起動するときの設定と、翻訳が終わった後のプロセスの扱いは[開発者向けの説明](docs/development.md#翻訳の起動とプロセスの後始末)にあります。

## テスト

```bash
python3 -m unittest discover tests
```

偽の翻訳コマンドを使うので、実際の Claude Code とネットワークは使いません。

実際の Claude Code が hook を呼ぶかどうかは、opt-in のライブテストで確かめられます。実際のモデルを呼ぶので、Claude Code のログインと料金（Sarashina では API キー）が要ります。

既定では、導入済みの設定（`~/.claude/hooks/claude-display-translate/settings.py`、`MSGDISP_CONFIG_DIR` があればその中）の `MODEL` で確かめ、設定がなければ haiku で確かめます。設定は実行せず、`MODEL` と `SARASHINA_API_KEY_FILE` の文字列の代入だけを読みます。

```bash
MSGDISP_LIVE=1 python3 -m unittest discover tests -p test_live_session.py
```

`MSGDISP_LIVE_MODEL=<名前>` でモデルを1つ選べ、`MSGDISP_LIVE_MODEL=all` で haiku と `translators/` の全モデルを順に確かめられます。all には Claude Code と Codex CLI のログインと Sarashina の API キーが要ります。

```bash
MSGDISP_LIVE=1 MSGDISP_LIVE_MODEL=all python3 -m unittest discover tests -p test_live_session.py
```

ライブテストでは次を確かめます。

- 実際の Claude Code が hook を呼ぶこと。
- hook の訳文が最終出力に使われること（`stream-json` の MessageDisplay hook の応答と `result` が一致すること）。
- 選んだ翻訳先が実際の入力で失敗しないこと（state の `translate.log` に `result=translated` が残ること）。

対話セッションでのバッチ分割、attended の判定（`MSGDISP_FORCE_ATTENDED` で迂回します）、画面の表示は確かめません。`MSGDISP_LIVE` がないときの `python3 -m unittest discover tests` ではスキップされます。

## ライセンス

[MIT](LICENSE)

Anthropic・OpenAI・SB Intuitions とは関係のない非公式ツールです。
