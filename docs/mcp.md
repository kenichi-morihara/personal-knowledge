# ChatGPT・Claude共通MCP

このMac上のghqリポジトリを原本として、OAuthで本人認証するStreamable HTTP MCPを提供します。初期構成はCloudflare Quick Tunnelによる一時HTTPS URLです。Macの起動・ネット接続とサーバープロセスが必要です。GitHubがprivateでも、接続したAIにはツール経由で原本のデータが渡ります。

## 起動

```sh
cd "$(ghq root)/github.com/kenichi-morihara/personal-knowledge"
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/start_mcp.py
```

`cloudflared`コマンドが必要です。このMacでは導入済みです。初回に出力される`https://...trycloudflare.com/mcp`が接続先です。URLに秘密鍵は含みませんが、所有者キーは誰にも送らないでください。自動起動や固定ドメインは設定していません。

停止：起動したターミナルでCtrl-C。または別ターミナルから次を実行します。

```sh
.venv/bin/python scripts/start_mcp.py --stop
```

再起動するとURLが変わり、認可済みトークンも消えます。各AIで古い接続を削除して新しいURLで追加・認証してください。

## ChatGPT

1. Plugins → Add → Add custom MCP serverを開く。
2. 名前を`Personal Knowledge`、Server URLを起動時のURL、AuthenticationをOAuthにする。
3. Advanced OAuth settingsではDCR（自動登録）を利用する。CIMDは未実装。読み書きする場合、Base scopesに`knowledge:read`、`knowledge:write`、`offline_access`を1行ずつ指定する。自動検出では読み取りだけが選ばれることがある。参照専用なら`knowledge:read`だけを指定する。
4. 内容と権限を確認してCreate as a plugin。所有者認証画面で、自分で始めた接続・正しい戻り先であることを確認して許可する。
5. 所有者キーはこのMacの次のコマンドでコピーできる。チャットには貼らず、生成した一時URLの所有者認証フォームにだけ貼り付ける。

```sh
pbcopy < "$HOME/Library/Application Support/personal-knowledge-mcp/owner-key.txt"
```

6. 接続・インストール後、チャットで`@Personal Knowledge`を選び「カナダ移住計画を検索して」と依頼する。

UIはアカウントによって異なります。[OpenAI公式のカスタムMCP設定](https://developers.openai.com/api/docs/guides/custom-mcp-server)に従ってください。ChatGPTから実際にツールが呼ばれたかを確認し、MCP未使用の推測回答を動作確認としないでください。

## Claude

Customize / Settings → Connectors → Add custom connectorで、同じURLを登録して認証します。OAuthクライアントの選択がある場合は自動登録（DCR）を利用します。所有者キーも同じです。設定したクライアントのスコープを超える操作はできません。

Claudeの画面での接続は未検証です。[Claude公式のリモート接続案内](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)を参照してください。

## 提供するツール

| ツール | 内容 |
| --- | --- |
| `search(query)` | 実レコードだけをキーワード検索。全語一致、最大20件。UUID・タイトル・原本URLを返す |
| `fetch(id)` | UUIDで全文・出典・パス・sha256を取得 |
| `get_template(kind)` | note / project / balance / obligation / opportunityの雛形。実データではない |
| `get_guidance()` | 現在の日本時間の日付と登録・金融・営業ルールを取得 |
| `put_record(path, content, expected_sha256)` | 明示的な保存依頼時にMarkdownを検証しコミット・push |

新規登録の`expected_sha256`は空文字、更新時は直前のfetch結果を使います。読み取りは`knowledge:read`、書き込みは追加で`knowledge:write`が必要です。送金・納税実行・メール送信・任意ファイル取得・シェル実行のツールはありません。

## 保存と同期

- 検索・取得はこのMacの追跡済み原本を参照し、テンプレート・README・未追跡ファイルを実データに含めません。
- 作業ツリーに未保存変更がある場合、MCPの書き込みを拒否します。ローカルAgentの作業を先に完了してください。
- 保存前にorigin/mainを確認します。別端末の更新がある場合、自動マージせず拒否するため、ローカルで`git pull --ff-only`し、再度fetchして更新します。
- ドラフトを一時コピーで検証してから原本を置き換えます。不整合・重複ID・リンク切れを検出した場合は原本を変更しません。
- 残高は新しいID・ファイルで追記。既存の支払い履歴はMCPから削除・書換不可です。訂正が必要な場合は理由を残してローカルで編集します。
- 保存はファイル単位です。複数レコードを一括更新するトランザクションや削除ツールはありません。
- push失敗時はローカルコミットを保全し` synced: false `を返します。ローカルで競合を確認してpushするまで未同期です。

## 認証と運用

- SDKのOAuth認可コード＋PKCE S256、動的クライアント登録を利用。認可画面は所有者キー、CookieとCSRF値、Origin検査で保護します。
- コールバックはChatGPT・Claudeの既知のホスト、ローカル検証用ループバックのみ許可します。異なる公式ホストが必要になった場合は明示的にレビューして追加します。
- 所有者キー・接続状態・ログはGit外の`~/Library/Application Support/personal-knowledge-mcp/`に保存。ディレクトリは所有者のみアクセス可能です。
- 認可・トークンはメモリ内だけに保持。アクセストークン1時間、offline_accessを付けた更新トークン7日間。更新時にローテーション、再起動で全失効します。
- URL変更・期限切れ・サーバー再起動後は再接続が必要です。接続解除はAI側から行うか、サーバー停止で全トークンを無効にします。
- 一時トンネルのデータ経路にはCloudflareが入ります。固定公開・常時運用に移す際は専用ホスティングと外部の認証基盤を検討します。

## 検証

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/validate.py
git diff --check
```

認証・HTTPツール呼出・Git保存は一時リポジトリの合成データで検証します。実際の税額・残高はテスト用に登録しません。
