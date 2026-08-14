#!/bin/sh
set -e

# Restore database from GCS if replica exists
if [ -f /app/litestream.yml ]; then
    echo "Restoring database from GCS if exists..."
    litestream restore -config /app/litestream.yml -if-replica-exists /app/data/fp_simulator.db || true
fi

# Start Litestream replication in background
litestream replicate -config /app/litestream.yml &
LITESTREAM_PID=$!

# Start the app
exec uvicorn fp_simulator.web.main:app --host 0.0.0.0 --port 8080 &
APP_PID=$!

# Wait for both processes (POSIX sh compatible)
wait $LITESTREAM_PID $APP_PID