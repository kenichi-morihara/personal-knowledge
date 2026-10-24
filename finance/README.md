# 金融・支払い

[残高雛形](../templates/balance.md)と[支払い義務雛形](../templates/obligation.md)を使います。UFJの確認済み表示残高は[2026-10-25のスナップショット](balances/mufg-20261025-034510.md)に記録しています。税金・国保・年金は[2026年・2027年の管理ページ](taxes/README.md)から確認します。金額と支払い実績は未確認です。

## 残高

`account_id`は口座番号ではなく自分用の固定識別子です。`observed_at`は残高を確認した時刻です。確認ごとに新規ファイルを作り、出典・通貨・確認日時を示して回答します。MoneytreeとUFJの連携は2026-10-25に再認証済みです。このリポジトリへの自動取得・同期はありません。

## 税金・支払い義務

1請求・1通貨につき1レコード。`amount_due`（請求額）、`amount_paid`（支払済額）、`amount_remaining`（未払額）、`due_date`（期限）を管理します。

- 初回は請求額と開始時点の支払済額`opening_paid`を本人・資料で確認する。未払いが確認できた場合だけ`opening_paid: "0"`とする。過去の支払い詳細が不明でも、確認済み累計をopening_paidに記録できる。
- 新規支払いは`payments`に固定ID、金額`amount`、実際の支払日`paid_at`、出典`source`を追記する。同じ領収書や取引の二重登録を確認する。
- `amount_paid = opening_paid + paymentsの合計`、`amount_remaining = amount_due - amount_paid`を同時に更新する。請求額や開始時点累計が不明なら、算出できない値はnullのままにする。
- `status`は未確認`unknown`、未払`unpaid`、一部支払`partial`、完了`paid`。確認済み金額から決める。過払い・還付・請求変更は自動処理せず事実確認を行う。
- 支払い記録は「支払った」という本人の依頼・証憑に基づく記帳であり、納税の実行ではない。
