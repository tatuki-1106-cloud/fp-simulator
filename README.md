# FPシミュレーター

FP-UNIVライクな日本のライフプラン・シミュレーター。月次キャッシュフロー計算、税制・社会保険・年金計算を行い、MCP(Model Context Protocol)サーバーとしてAIエージェントからの参照・計算・更新を可能にする。

### 収入・産休・育休

Q2の各収入に年間昇給率と産前産後休業・産後パパ育休・育児休業を登録できます。年間昇給率は基準年の月額・賞与を基準に毎年複利で反映します。給与・賞与・給付金は暦日で日割りし、給付金は生活設計用の月次受給相当額としてキャッシュフローへ反映します。休業期間が月末を含む月は社会保険料を全額免除する簡略モデルです。実際の支給日、会社ごとの賞与規定、個別の給付上限・受給可否は再現しないため、申請や確定申告の判断には利用しないでください。

### iDeCo・NISA

Q11でiDeCo・NISAを登録できます。

- **iDeCo**: 掛金は全額所得控除(小規模企業共済等掛金控除)として所得税・住民税へ反映します。受取は「一時金」「年金」「一時金+年金」を選択でき、一時金は加入年数ベースの退職所得控除による分離課税、年金受取は老齢年金と合算した公的年金等の雑所得として課税します。年金受取は受取期間(年数)を指定できます。退職金と一時金の退職所得控除の重複(19年・5年ルール)は年単位の簡易近似で調整します(暦日単位の厳密判定・同一年受取の合算計算は行いません)。
- **公的年金の課税**: 老齢年金・iDeCo年金受取は公的年金等控除(国税庁No.1600)を適用した雑所得として所得税・住民税を計算します。
- **NISA**: つみたて投資枠(年120万円)と成長投資枠(年240万円・生涯1,200万円)を分離管理し、つみたて枠の超過分は成長枠へ自動振替します。生涯非課税枠(1,800万円)は簿価ベースで消費し、売却分の簿価は翌年に復活します。初期残高は簿価=評価額とみなします。

### 乗り物

Q7では、自動車・バイクなどについて、所有期間、購入・維持・車検・買替を設定できます。車両区分は普通乗用車・軽自動車・二輪車から選択できます。追加で、月間走行距離と燃費/電費・単価からガソリン代または電気代を月次計算し、自動車税（年額）と重量税（車検時）の減税率を適用できます。売却額は手入力のほか、残価率・定額法・定率法から自動計算できます。税額は車検証・自治体通知などで確認した減税前金額を入力する簡易モデルで、制度上の車種区分や減税要件を自動判定するものではありません。二輪車の自動車税は排気量区分から概算し、重量税は手入力します。

### 教育費

Q5では、子どもごとに保育園・幼稚園・小学校・中学校・高校・大学の進学先を段階別に設定できます。公立/私立の標準パスに加えて、国立・私立文系・私立理系・専門学校・未定を選択できます。保育園は認可保育所の費用目安を初期値として扱い、自治体や世帯所得で変動しやすいため、必要に応じて個別年間費用へ更新してください。「未定」は選択した標準パスを仮置きするため、後から段階別に修正できます。

各段階は公的平均または個別年間費用を選べます。入学金、教材・通学等の年額、支援・奨学金、大学などの一人暮らし費用を追加でき、習い事・塾は開始年齢、終了年齢、月額を個別に設定できます。教育費専用の年率も設定でき、結果画面では子ども別・年別の内訳を表示します。公的平均は目安であり、学校・地域・家庭ごとの実額とは異なるため、判明した費用は個別入力へ更新してください。

## ドキュメント

- [docs/要件定義書.md](docs/要件定義書.md) — 機能要件・非機能要件・アーキテクチャ
- [docs/index.html](docs/index.html) — 初心者向け学習ロードマップ（リポジトリの読み方・機能追加・不具合修正）

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
