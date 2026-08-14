# FPシミュレーター (Cloud Run用)
FROM python:3.12-slim

WORKDIR /app

# Litestreamのインストール(SQLite→GCSレプリケーション)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -L https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz | tar xz \
    && mv litestream /usr/local/bin/ \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 依存のインストール(レイヤーキャッシュのため先にコピー)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# パラメータとテンプレート
COPY parameters ./parameters
COPY deploy/litestream.yml /app/litestream.yml

# SQLiteデータディレクトリ(Cloud Runでは /tmp またはインメモリ。LitestreamでGCSへレプリケーション)
# staticディレクトリも作成(パッケージに含まれない場合のため)
RUN mkdir -p /app/data && mkdir -p /usr/local/lib/python3.12/site-packages/fp_simulator/web/static

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=8080 \
    FP_DB_PATH=/app/data/fp_simulator.db \
    FP_PARAMETERS_DIR=/app/parameters

EXPOSE 8080

# LitestreamでGCSからリストアしてからuvicornを起動し、バックグラウンドでレプリケーション
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
