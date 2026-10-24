# Beancount＋Fava：支払い管理

Beancountで税金・保険料などの請求と支払いの整合性を検証し、Favaで一覧を見ます。原本は従来どおり[金融レコード](../README.md)のMarkdownです。このディレクトリは独立した読み取り用の帳簿生成機能です。既存のMCPやMarkdownの形式は変更しません。

## セットアップと使い方

リポジトリのルートで実行します（Python 3.11以上。初期検証は3.14）。

```sh
make ledger-setup  # 初回のみ
make ledger-check
make ledger-summary
make fava
```

`make fava`後、ブラウザで `http://127.0.0.1:5001` を開きます。Ctrl+Cで終了。ローカルだけに読み取り専用で公開します。`make ledger-build`は帳簿生成のみ、`make ledger-check`と`make ledger-summary`は原本も生成ファイルも変更しません。summaryはAgentから扱えるJSONです。`make`でコマンド一覧を表示します。Pythonの選択は`make ledger-setup PYTHON=python3.12`のように変更できます。

## Agentの運用

1. ルートのAGENTS.mdと[金融ルール](../README.md)を読む。
2. finance配下の既存の`type: obligation`を検索する。通知書・本人申告を根拠に、[雛形](../../templates/obligation.md)で登録・更新する。
3. 支払いは元レコードのpaymentsに固定IDと支払日・金額・出典を追記し、集計値を整合させる。銀行口座不明なら推測しない。
4. ルートの検証に加えて、`make ledger-check`を実行し、`make ledger-summary`で未払額を確認する。出典ファイルと確認日を回答に含める。
5. 原本の変更後は`make fava`を再起動してFavaの表示を更新する。Favaの編集機能は無効です。生成ファイルはGit対象外で、次回生成時に置き換わる。

## 表示の意味と限界

- 請求・支払済額・未払額・通貨がすべて確認済みのレコードだけを帳簿に含める。不明値のある記録はゼロ扱いせず、summaryの`skipped_unknown`に出す。
- 1請求ごとに独立した負債勘定を作る。負債のマイナス残高が未払額。JPYとCADなどを換算・合算しない。
- 帳簿上の日付は原本のupdated_at（記録更新日）。過去の課税日・支払日を推定しない。実際の支払日はpaymentの`paid-at`メタデータに保持する。したがってFavaの時系列は実際の支払い推移を表さない。
- 支払い元口座は原本では未指定なので、相手勘定は`Equity:UnallocatedPayments`。UFJなどの銀行残高は減らさず、残高スナップショットも合算しない。
- `Expenses:RecordedObligations`は管理表示用。税務上の必要経費であることを意味しない。これは正式な申告帳簿、日本の税額計算、申告・納付実行、期限通知を提供するものではない。
- 期限と根拠は原本とsummaryで確認する。生成帳簿にはdue-date、source-file、record-id、支払いID等を保持する。

## 検証

```sh
make ledger-test
```

テストは一時ディレクトリの架空データで実行し、実データへ混入させません。初期状態では金融データがないため、Favaは空の帳簿を表示します。

[Beancount](https://github.com/beancount/beancount)／[Fava](https://beancount.github.io/fava/)
