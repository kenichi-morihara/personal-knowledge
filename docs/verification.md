# 初期検証記録

検証日：2026-10-25（Asia/Tokyo）

- GitHub `kenichi-morihara/personal-knowledge` の `isPrivate: true` を確認。
- ghq配置：`/Users/kenichi.morihara/ghq/github.com/kenichi-morihara/personal-knowledge`。
- 実データは `projects/canada-2027.md` の1件。対象年2027、ID `9c7a7dbf-5d35-47a1-a2fd-30f51ecad091`、更新日2026-10-25。金融・営業の実データは未登録。
- この構築を行ったCodexセッションから実ファイルを読み、対象年・ID・未確認事項を確認。別のCodex CLIセッション起動は未検証。
- Claude Codeはインストール・認証済みだが、読み取り検証の起動で組織設定による拒否：`Your organization has disabled Claude subscription access for Claude Code`。回答は生成されず、Agentからの参照は未検証。CLAUDE.mdの共通ルール参照は配置済み。
- 9件の一時データテストで登録・検索・更新、一部支払い・完済、未知値、二重ID、出典欠落、雛形混入、JPY小数、タイムゾーン、リンク切れを確認。実データディレクトリに検証用レコードは残さない。
- 実データの検証と `git diff --check` が成功。

再検証：

```sh
.venv/bin/python scripts/validate.py
.venv/bin/python -m unittest discover -s tests -v
```

Claude Codeの利用が有効になったら、このリポジトリを作業ディレクトリとして起動し、ファイル変更なしでカナダ移住の対象年・ID・確認日・未確認事項と金融データ件数を問い合わせる。テンプレートを実データに数えないことも確認する。

## MCP追加時の検証（2026-10-25）

- 合成データの一時Gitリポジトリで既存9件＋MCP関連14件、合計23件のテストが成功。
- OAuth discovery、PKCE、所有者キー・CSRF検証、認可コード再利用拒否、更新トークンのローテーション・失効、読み取り専用権限を確認。
- HTTP経由の登録・検索・取得、Gitへの保存・push、古いハッシュ・未保存変更・リモート先行更新の拒否、残高と支払い履歴の追記制限を確認。
- Cloudflareの一時HTTPS URLで未認証拒否、OAuth認証、MCP initialize、既存カナダ計画の検索・取得、トークン失効を実測。実データの登録・変更はしていない。
- ChatGPTブラウザでカスタムMCP追加フォームを準備。アカウントへの接続許可とAIからの実利用確認は、この記録時点では未完了。Claudeブラウザ接続も未検証。
- 起動・停止・URL再登録・所有者認証手順は[MCP手順](mcp.md)を参照。
