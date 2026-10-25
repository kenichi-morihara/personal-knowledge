# 内訳編集ページ（ローカル）

既存の画面案を保護するため、独立した編集ページとローカルAPIを追加しています。原本は同じ`db.json`です。既存の`index.html`・`quest.fragment.html`は変更していません。

## 起動

リポジトリのルートで：

```sh
.venv/bin/python scripts/funding_roadmap.py --port 8863
```

`http://127.0.0.1:8863/`を開きます。Ctrl+Cで停止。Python標準ライブラリのみを使用します。外部公開・リモートMCP・撮影サーバーの設定変更はありません。

## 編集

- イベントの「内訳を編集」から、項目名・円単位の金額・確認状態・メモ・出典を入力。行の追加・削除ができます。
- 旧総額は「未分類残額」として残します。新しい明細の金額をそこから振り分けるので、合計を二重に増やしません。元の総額を超えた場合は合計と差額が増えます。
- 未分類残額を削除すると、明細の合計だけになります。空欄の金額を含む合計は未確認。すべての明細を削除した場合も未確認です。「イベント費用なし」を明示した地点だけ0円になります。
- 「毎月の収入・支出」では項目を自由に追加・編集・削除でき、適用開始月と終了月を設定できます。
- 月次収支の「この月を編集」ではその月全体の金額を上書きします。基本設定は変わりません。日割り表示の途中期間だけの金額ではありません。月別変更を解除すれば基本設定へ戻ります。
- ダイアログの「変更を適用」は画面内だけです。最後に上部の「DBに保存」を押すと原本とローカルGitへ保存します。キャンセルはダイアログ内の変更を破棄します。

## DBと移行

GETでは旧version 2のJSONをメモリ内でversion 3に変換するだけで、原本は変更しません。最初の保存でversion 3になります。既存のイベントUUID・日時・出典・元の参考額は維持します。金額は整数の円、未確認はnullです。

- `events[].items[]`：固定UUID、label、amount、status、note、source、updated_at。合計は明細から算出。`legacy_total`は移行前の参考総額です。
- `monthly_rules[]`：固定UUID、label、direction（income/expense）、amount、start_month、end_month、status、note、source、updated_at。
- `overrides[]`：固定UUID、rule_id、month、amount、note、source、updated_at。設定ごと・月ごとに1件。月額を差し替えます。
- `revision`・`updated_at`：保存時に更新。`opening_balance`は計画用仮残高で、銀行とは非連携です。

旧画面のlocalStorageとは共有しません。旧画面は固定項目の試作で、追加明細・自由な月次項目・月別変更のversion 3モデルに対応していないため、編集後の計画確認はこの編集ページを使ってください。旧画面への統合は別作業です。

## 保存APIと競合

- `GET /api/plan`：version 3の計画、原ファイルのSHA-256（etag）、CSRF値を取得。
- `PUT /api/plan`：計画JSONと`If-Match`・`X-CSRF-Token`を送信。更新番号と原本のハッシュを照合し、競合は409で拒否。
- ループバック専用、Host・Origin検査、1MB制限。UIは同じオリジンの通常ページとして動作し、既存iframeのsandbox設定を緩めません。
- DBに未コミットの編集がある場合は保存を拒否。main以外では保存しません。
- 保存はDBファイルだけのGitコミット。他ファイルの編集中・ステージ済み変更は含めません。失敗した場合はDBを復元。
- GitHubへのpush・fetch・mergeは行いません。画面は「GitHub未同期」と表示します。別途通常のGit操作で同期してください。
- Agentが原本を編集する場合はversion 3を維持し、内訳を原本として扱います。旧`amount_wan`へ戻さないでください。

## 検証

```sh
.venv/bin/python -m unittest discover -s tests -p test_funding_roadmap.py -v
node tests/test_funding_model.mjs
```

一時Gitリポジトリと架空データで、移行、nullと0の区別、重複ID、月別変更、楽観ロック、HTTPのHost/Origin/CSRF、DB保存、コミット失敗時の復元、他のステージ済み変更の保護を確認します。月の日割りは累計の丸め差分で配分し、イベントをまたいでも1か月分が二重計上・端数欠落しないようにしています。
