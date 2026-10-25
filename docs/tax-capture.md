# Androidで納付書を撮る

専用ページから写真を撮ると、Mac内のApple Visionで文字認識し、税目・金額・年度・納期限の候補をprivate Gitに「要確認」のメモとしてコミット・pushします。写真を選んだ時点で送信・記録が始まります。実際の請求と支払いは自動確定しません。

## 初回セットアップ

```sh
cd "$(ghq root)/github.com/kenichi-morihara/personal-knowledge"
make capture-setup
make capture-start
```

Mac、Xcode Command Line Tools（Swift）、Python 3.11以上、cloudflaredが必要です。このMacではセットアップ済み。起動したターミナルを維持し、スリープさせないでください。別ターミナルの`make capture-status`でもURLを確認できます。

1. 表示された`https://…trycloudflare.com`をAndroidのChromeで開く。
2. Macで`make capture-key-copy`を実行し、キーを自分の端末間で安全に転送してページに入力する。AIチャットには貼らない。これはMCPの所有者キーとは別です。
3. ログイン後、Chromeのメニューから「ホーム画面に追加」。専用の「納付書を撮る」入口として使う。
4. 「カメラで納付書を撮る」から、用紙全体を1枚ずつ撮る。保存済み画像も選べます。15MB以下のJPEG・PNG、40メガピクセル以下。
5. 「撮影記録を保存しました（要確認）」「GitHubにも保存済み」を確認する。読み取れない項目・複数の候補がある項目は未確認になる。

Quick Tunnelは再起動でURLが変わります。ホーム画面の入口も新しいURLで作り直してください。固定URLやOSログイン時の自動起動は未設定です。

## 確認して請求原本に反映

```sh
make capture-pending
```

Agentには「要確認の納付書を確認して」と依頼できます。候補は`finance/taxes/captures/`にあります。原画像・OCR全文は、このMacの`~/Library/Application Support/personal-knowledge-capture/`の`images/`と`ocr/`に、画像SHA-256を名前として保存します。確認に必要な画像を開く場合はこのローカル原本を使い、原画像や全文をGitにコピーしないでください。

- 人間が用紙と候補を照合し、請求総額・期別・課税年度・納期限・既納額を確認する。
- 既存の請求原本を検索して二重計上を避ける。既に登録済みなら原本への関連づけだけ行う。
- [税金管理ルール](../finance/taxes/README.md)に従い、確認できた内容を`type: obligation`の請求原本に登録・更新する。不明な支払額を0にしない。
- 撮影メモの`capture_status`を`reviewed`に変更し、`confirmed_record_id`を付けて本文から請求原本に相対リンクする。反映を見送る場合は`dismissed`と理由を残す。
- `make ledger-check`で検証する。確定していない撮影メモはFavaに計上しない。Favaは原本反映後に再起動する。

## 二重撮影・失敗時

- 同一画像はSHA-256で重複排除し、再送しても同じ記録を返す。
- 撮り直した画像はOCR全文の指紋、税目・金額・年度・期限で既存の撮影記録と照合する。確認済み請求との同条件一致も重複候補として示す。自動で請求を統合しない。
- 写真は記録前にローカルへ保存する。OCRやGitが失敗したら、接続・作業ツリーを確認して同じ写真を再送できる。
- GitHubへpushできなくても、ローカルコミットは保全する。「Macに保存済み」の場合はMacでGit状態を確認してpushする。
- ローカルの未コミット変更や、未取得のリモート更新がある間は、新規記録を拒否する。作業を保存し、必要なら`git pull --ff-only`で同期してから再試行する。
- 撮影結果のパスとIDを画面に表示する。ネット切断後に「記録済み」が返っても、撮影メモを要確認として扱う。

## 接続と保管

撮影サーバーはループバックの8780番で待ち受け、専用のCloudflare HTTPSトンネルで公開します。既存MCPとは別のプロセス・ポート・認証キーで、既存MCPを再起動しません。写真の転送経路にはCloudflareが入り、OCR処理自体はMac内です。生画像・OCR全文・キー・ログはGit外です。画像は自動削除しません。

30日間のHttpOnly・Secure・SameSiteセッション、Origin・CSRF検査、ログイン試行制限、画像サイズ上限を使用します。MCPや撮影キーを共有する相手には、それぞれの機能へのアクセスが渡るため、本人専用として扱ってください。

```sh
make capture-stop   # 撮影接続のみ停止。既存MCPは停止しない
make capture-test
```

停止後に端末紛失等で撮影キーを失効させたい場合は、MacでGit外の`capture-key.txt`を削除して`make capture-setup`し、サーバーを再起動してください。以前のセッションも失効します。

Android実機のカメラ起動・ホーム画面追加は、実機で最初の1枚を撮影して確認してください。
