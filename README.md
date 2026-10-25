# personal-knowledge

自分が所有する、Agentに依存しないパーソナルナレッジ。原本はこのprivate GitリポジトリのMarkdownです。

## 使い始める

```sh
cd "$(ghq root)/github.com/kenichi-morihara/personal-knowledge"
codex
# または
claude
```

最初に[共通ルール](AGENTS.md)を読ませ、「2027年のカナダ移住計画を教えて」「この情報を記録して」と依頼します。他の作業ディレクトリから使う場合は、このリポジトリの絶対パスとAGENTS.mdを明示してください。各Agentのアクセス制限に従います。

| 場所 | 内容 |
| --- | --- |
| [profile](profile/README.md) | プロフィール・応答の好み・現在の関心 |
| [finance](finance/README.md) | 口座残高スナップショット・税金・支払い履歴 |
| [projects](projects/README.md) | 計画・タスク。まず[カナダ移住](projects/canada-2027.md) |
| [sales](sales/README.md) | 営業案件・商談記録・次のアクション |
| [notes](notes/README.md) | 分類前の情報・一般メモ |
| [templates](templates/README.md) | コピーして使う入力雛形（実データではない） |

## 原本の形式

1レコード1ファイル。YAML frontmatterに`id`、`type`、`updated_at`、`sources`を置き、本文に背景・判断理由・関連レコードへの相対リンクを残します。IDは作成時にUUIDを付けて固定し、ファイル名を変更しても維持します。

日付はYYYY-MM-DD、時刻はタイムゾーン付きISO 8601。未確認の構造化値は`null`、本文では「未確認」と表記します。金額は引用符つきの十進文字列（例：`"12345"`）とISO通貨コード（例：JPY、CAD）を使い、異なる通貨を合算しません。JPYは整数です。

出典には本人申告・文書名・URL等と取得日を記録します。取得日と、金融データ自体の確認日時`observed_at`は別です。API未連携の残高を「現在残高」として扱いません。

## 登録・更新

- 既存ID・名前を検索してからテンプレートをコピー。雛形は検索・集計の対象外です。
- 本人から依頼された内容だけ記録し、不明値を埋めません。新しい記録には出典を追加します。
- 支払い義務と支払い履歴は[financeの手順](finance/README.md)に従います。
- 変更を検証し、差分を確認して、自身の変更だけをコミット・pushします。競合時は上書きせず調整します。

## 検証

Python 3.11以上とPyYAMLを使います。仮想環境はGit対象外です。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/validate.py
.venv/bin/python -m unittest discover -s tests -v
```

実データはprivate Gitに保存しますが、認証情報・トークン・銀行ログイン情報・生の取込資料は保存しません。生資料はリポジトリ外に保管し、必要な確認済み情報と資料名だけを記録します。Git履歴は通常の削除後も残ります。

## ChatGPT・Claudeから使う

認証付きMCPを追加しました。[接続・起動手順](docs/mcp.md)に従い、このMacから一時HTTPS URLを発行できます。検索・取得・依頼した登録／更新を共通のGit原本に対して行います。Macとサーバープロセスの稼働が必要です。外部DB・銀行自動取得・通知・送金や納税実行は未実装です。

UFJ残高はMoneytree Webの表示を確認し、[確認日時付きの記録](finance/balances/mufg-20261025-034510.md)として保存しました。リポジトリへの自動同期は未実装です。[公式CSV明細出力](https://direct.bk.mufg.jp/fw/ib_help/meisai.html)は利用可能ですが、自動取込は未実装です。[MoneytreeのChatGPT連携](https://getmoneytree.com/press-release-jp/apps-in-chatgpt)は、当リポジトリやClaudeとの同期とは別機能です。

初期の確認結果とClaude Codeの未検証事項は[検証記録](docs/verification.md)を参照してください。

## 納付書をスマホで撮影

Androidの専用撮影ページからMac内OCRで読み取り、「要確認」として保存します。[使い方](docs/tax-capture.md)。`make capture-start`で起動し、`make capture-pending`で確認待ちを一覧にできます。未確認の撮影記録は債務や支払いに自動計上しません。
