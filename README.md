# FPシミュレーター

FP-UNIVライクな日本のライフプラン・シミュレーター。月次キャッシュフロー計算、税制・社会保険・年金計算を行い、MCP(Model Context Protocol)サーバーとしてAIエージェントからの参照・計算・更新を可能にする。

## ドキュメント

- [docs/要件定義書.md](docs/要件定義書.md) — 機能要件・非機能要件・アーキテクチャ
- [docs/learning-path.html](docs/learning-path.html) — 初心者向け学習ロードマップ（リポジトリの読み方・機能追加・不具合修正）

### GitHub Pages

学習ロードマップはGitHub Pagesでも閲覧できます。

- 公開URL: <https://tatuki-1106-cloud.github.io/fp-simulator/>
- `.github/workflows/pages.yml` が `master` ブランチの `docs/` を自動デプロイします。
- 初回のみ、GitHubリポジトリの **Settings → Pages → Build and deployment → Source** で
  **GitHub Actions** を選択してください。

## セットアップ

```bash
# 依存インストール(開発用)
pip install -e ".[dev]"

# テスト
pytest

# 開発サーバー起動
uvicorn fp_simulator.web.main:app --reload --port 8000
```

## MCPサーバー

AIエージェント(Claude等)からライフプランの参照・計算・更新をツールとして呼べます。

**利用可能なツール:**
- `list_households` / `get_household` — 世帯の参照
- `update_household` — 世帯データの作成・更新
- `run_simulation` — シミュレーション実行(サマリー返却)
- `get_cashflow` — 指定年月のキャッシュフロー明細
- `explain_amount` — 金額の計算根拠(トレーサビリティ)
- `list_tax_parameters` / `get_tax_parameter` — 税制パラメータ参照

**接続方法:**
- HTTP: `http://localhost:8000/mcp/` (Streamable HTTP)
- stdio: `python -m fp_simulator.mcp_server.stdio_main`

## 構成

```
src/fp_simulator/
  engine/       # 計算エンジン(純粋関数)
  parameters/   # 税制パラメータローダー
  db/           # SQLite永続化
  web/          # FastAPI + Jinja2 + HTMX
  mcp_server/   # MCPエンドポイント
parameters/     # 税制パラメータYAML(時系列・出典付き)
tests/          # pytest(回帰テスト含む)
deploy/         # Dockerfile / Cloud Run / Litestream
```

## デプロイ(Google Cloud Run)

```powershell
# 1. GCPプロジェクトを作成し、gcloud を認証
# 2. Artifact Registry リポジトリを作成
gcloud artifacts repositories create fp-simulator --repository-format=docker --location=asia-northeast1

# 3. デプロイ
.\deploy\deploy.ps1 -ProjectId <GCPプロジェクトID>
```

### 認証(IAP)

Cloud RunのIAPを有効化し、許可するGoogleアカウントを設定してください。Cloud Runの`run.app` URLへ直接IAPを適用するため、ロードバランサや独自ドメインは不要です。

Web UIはIAPの `x-goog-authenticated-user-email` を世帯所有者として保存し、所有者以外の世帯を拒否します。ローカル開発では認証を無効化しています。

MCPはAPIキー必須です。`FP_MCP_API_KEY` にSecret Managerの値を設定し、`Authorization: Bearer <APIキー>` または `X-API-Key` で接続してください。Cloud Runデプロイスクリプトは `fp-mcp-api-key` Secretを参照します。

既存世帯を自分のアカウントへ移行する場合は、デプロイ時に `-OwnerEmail <Googleアカウント>` を指定してください。所有者未設定の世帯だけが一度割り当てられます。

#### Gmailなど組織外アカウントでIAPを使う場合

Google管理OAuthでは組織外アカウントを追加できないため、IAPでカスタムOAuthを一度だけ設定します。

1. Google Auth Platformで「ウェブ アプリケーション」のOAuthクライアントを作成します。
2. OAuthクライアントの承認済みリダイレクトURIに、次のURIを登録します（`<CLIENT_ID>`は作成したクライアントIDに置き換えます）。

   `https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect`

3. Cloud Runサービスの「セキュリティ」→「IAP」→「ポリシーの編集」→「IAPで構成」で、カスタムOAuthを選択し、クライアントIDとシークレットを保存します。
4. 同じIAPポリシーで、利用者に `roles/iap.httpsResourceAccessor` を付与します。

OAuthクライアントの設定反映には数分かかる場合があります。

### バックアップ(Litestream)

`deploy/litestream.yml` を参照。Cloud StorageバケットへSQLiteを継続レプリケーションします。

バックアップの存在確認:

```powershell
gcloud storage ls --recursive gs://fp-simulator-backup-168688416857/**
```

復旧確認は、本番サービスとは別の一時Cloud Run Jobで実行します。実行後は検証用Jobを削除してください。

```powershell
$image = "asia-northeast1-docker.pkg.dev/fp-simulator/fp-simulator/app:latest"
gcloud run jobs create fp-simulator-backup-verify `
  --image $image --region asia-northeast1 --project fp-simulator `
  --service-account 168688416857-compute@developer.gserviceaccount.com `
  --command litestream `
  --args "restore,-config,/app/litestream.yml,-if-replica-exists,/app/data/fp_simulator.db" `
  --max-retries 0 --task-timeout 5m
gcloud run jobs execute fp-simulator-backup-verify `
  --region asia-northeast1 --project fp-simulator --wait
gcloud run jobs delete fp-simulator-backup-verify `
  --region asia-northeast1 --project fp-simulator --quiet
```

`database not found in config` が出る場合は、リストア先を `/app/data/fp_simulator.db` にしてください。Litestream設定のDBパスと一致している必要があります。

### 運用監視

Cloud Runの5xxレスポンスをCloud Monitoringで監視します。アラートポリシーは次のファイルから作成できます。

```powershell
gcloud monitoring policies create `
  --project fp-simulator `
  --policy-from-file deploy/monitoring/cloud-run-5xx-alert-policy.json
```

通知先を追加する場合は、Cloud Monitoringの「アラート」→「通知チャンネル」からメール等を登録し、作成したポリシーに割り当てます。メール通知は所有者による確認が必要です。

`/healthz` はIAP/Cloud Runフロントエンド経由で汎用404になる環境があるため、現時点では公開Uptime Checkの対象にしません。可用性はCloud Runのリクエスト・エラーメトリクス、ログ、リビジョン状態で確認します。
