# FPシミュレーター Cloud Run デプロイスクリプト
# 使い方: .\deploy\deploy.ps1 -ProjectId <GCPプロジェクトID> [-Region asia-northeast1]
#
# 前提:
#   - gcloud CLI がインストール・認証済み
#   - Artifact Registry リポジトリ "fp-simulator" が作成済み
#   - Cloud Storage バケット "<ProjectId>-fp-simulator-backup" が作成済み(Litestream用)
#   - IAPの設定は別途(README参照)

param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "asia-northeast1",
    [string]$ServiceName = "fp-simulator",
    [string]$McpApiKeySecret = "fp-mcp-api-key",
    [Parameter(Mandatory = $true)][string]$OwnerEmail
)

$ErrorActionPreference = "Stop"

$GcloudCommand = Get-Command gcloud.cmd -ErrorAction SilentlyContinue
if ($null -eq $GcloudCommand) {
    $localGcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
    if (-not (Test-Path $localGcloud)) {
        throw "gcloud CLI was not found."
    }
    $GcloudPath = $localGcloud
} else {
    $GcloudPath = $GcloudCommand.Source
}

$Image = "$Region-docker.pkg.dev/$ProjectId/fp-simulator/app:latest"

Write-Host "=== 1. Dockerイメージのビルド&プッシュ ===" -ForegroundColor Cyan
& $GcloudPath builds submit --tag $Image --project $ProjectId

Write-Host "=== 2. Cloud Run へデプロイ ===" -ForegroundColor Cyan
& $GcloudPath run deploy $ServiceName `
    --image $Image `
    --region $Region `
    --project $ProjectId `
    --platform managed `
    --no-allow-unauthenticated `
    --iap `
    --max-instances 1 `
    --set-env-vars "FP_PARAMETERS_DIR=/app/parameters,FP_DB_PATH=/app/data/fp_simulator.db,FP_REQUIRE_IAP_AUTH=true,FP_LEGACY_OWNER_EMAIL=$OwnerEmail" `
    --set-secrets "FP_MCP_API_KEY=${McpApiKeySecret}:latest"

Write-Host "Deployment completed." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Configure IAP and allowed Google accounts."
Write-Host "  2. Configure the Litestream GCS bucket and credentials."
Write-Host "  3. Ensure the Cloud Run service account can access '$McpApiKeySecret'."
