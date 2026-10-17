# claude-display-translate

Claude Code が英語で返したメッセージを、画面表示だけ日本語に差し替える `MessageDisplay` hook です。

- 英語と判定したメッセージを `claude -p`（Claude Haiku 5.5、effort low）で日本語に訳して表示します。
- 日本語のメッセージはそのまま表示します。
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

この例のように、コードブロックはそのまま表示され、文中の `npm ci`・`package.json` と URL も訳されずに残ります。

## メリット

### メインセッションのコンテキストを汚さない

訳文は画面に出るだけで、会話履歴にも Claude に送る内容にも入りません。
`MessageDisplay` hook は表示を差し替えられるだけで、会話履歴（transcript）に保存される内容や Claude に送る内容は変えられません。
そのため、英語のメッセージが何件あっても、訳文のぶん会話履歴が増えることはありません。
[Claude Code の公式ドキュメント](https://code.claude.com/docs/ja/hooks)

Stop hook で訳文を Claude に返す方式では、訳文や翻訳の指示が会話に入り、そのぶんコンテキストを消費します。

また、翻訳は別に起動する `claude -p`（Claude Haiku 5.5）が行うので、メインセッションのモデルは翻訳に使われません。

- 英語の原文の代わりに訳文が表示され、原文と訳文が並んで画面が長くなりません。
- コマンドとコードは訳さないので、表示されたままコピーして使えます。

## 注意

- 英語のメッセージ1件ごとに Haiku の呼び出しが1回増えます。サブスクリプションの利用枠または API 料金を消費します。
- 訳文は、メッセージの生成が終わってから表示されます。
- セッションを再開すると（`claude --resume`、`claude --continue`、`/resume`）、再開前に訳文で表示していたメッセージは、英語の原文で表示されます。会話履歴（transcript）には訳文ではなく原文が保存されていて、再開時はこの hook を通さずに表示されるためです。
- 翻訳に失敗したときは `[翻訳失敗: 原文を表示]` を付けて原文を表示します。閉じたコードブロックを除いて8000文字を超えるときは `[長文のため原文を表示]` を付けて原文を表示します。メッセージの最後で初めて英語と判定した長文には、印を付けずに原文を表示します。
- 翻訳が timeout した後の5分間は翻訳しません。
- API キーやトークンに見える文字列を含むメッセージは、翻訳に送りません。ただし閉じたコードブロック（`` ``` `` や `~~~`）の中は、検査も翻訳への送信もしません。閉じていないコードブロックやインデントで書いたコードは、本文と同じく検査して送ります。
- 対話セッションでだけ動きます。`claude -p` と Agent SDK では動きません。

## 翻訳にかかる時間

メッセージの生成が終わってから訳文が出るまでの目安です。作者の環境で記録した約200件の実測値（Claude Haiku 5.5、effort low）で、
時間は翻訳用の `claude -p` の実行時間、文字数は訳文の長さです。文字数は、閉じたコードブロックを短いプレースホルダーに置き換えた状態で数えています。

| 訳文の長さ | 件数 | 中央値 | 最大 |
|---|---|---|---|
| 300文字未満 | 194 | 2.0秒 | 5.2秒 |
| 300〜1000文字 | 5 | 4.6秒 | 5.6秒 |
| 1000〜2000文字 | 3 | 7.0秒 | 8.1秒 |

2000文字を超える訳文の実測値はありません。

## 動作要件

- Claude Code（2.1.301 で動作確認。`MessageDisplay` hook が必要）
- Python 3（標準ライブラリのみ。3.14 で動作確認）
- Linux、macOS、WSL。Windows ネイティブには対応しません（`fcntl` を使うため）。

## 導入手順

Claude Code の公式ドキュメントと同じ流れで、
設定ファイルに hook を追加します。Claude Code に頼んでも、手動でも、同じ4つの手順で導入できます。
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

   `timeout` を省くと Claude Code が10秒で hook を打ち切ってしまい、表示を保留していた英語の行が画面に出なくなります。翻訳には長いと数十秒かかり、hook 内部の上限も55秒となっているため、`timeout` には必ず `60` を指定してください。

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
| `MSGDISP_CLAUDE_CMD` | Claude コマンドを JSON 配列で差し替える（テスト用） |
| `MSGDISP_FORCE_ATTENDED` | 非対話セッションでも動かす |

## 状態とログ

状態は `~/.claude/hooks/state/msgdisp/` に置きます。所有者だけがアクセスできる権限（700）にします。

- 言語を判定するまでの行と、表示を保留している行の本文を、メッセージごとにここへ一時保存します。メッセージの最後で削除します。
- Claude Code が途中で終了するなどして残った分は、別のメッセージの最後に、最後の更新から1時間たったものを削除します。
- 翻訳する間だけ、原文を OS の一時ディレクトリに所有者だけが読めるファイルとして書き、翻訳が終わると削除します。
- `translate.log` には結果の分類と経過秒を記録し、翻訳に成功したときと長文で翻訳を見送ったときは文字数も記録します。本文は記録しません。ただし訳文の検証に失敗したときは、原文の inline code か URL を1つ記録します。

## 仕組み

1. ストリーミング中のメッセージを英語と判定した時点で、以降の行の表示を保留して `[自動翻訳]` を表示します。
2. メッセージの最後で、保留した全文のうち閉じたコードブロックと区切り用のタグをプレースホルダーに置き換え、system prompt として `claude -p` に渡します。
3. 訳文で段落・リスト・見出しが大きく欠けていないこと、原文の inline code と URL が訳文に含まれていることを確かめてから表示します。

翻訳用の `claude -p` は hook、tool、MCP、セッション保存を無効にして起動するので、翻訳が再帰しません。

## テスト

```bash
python3 -m unittest discover tests
```

偽の翻訳コマンドを使うので、実際の Claude Code とネットワークは使いません。

## ライセンス

[MIT](LICENSE)

Anthropic とは関係のない非公式ツールです。
