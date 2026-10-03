#!/bin/sh
set -eu
# Only these public values are written into browser assets.
jq -n --arg googleClientId "${FRONTEND_GOOGLE_CLIENT_ID:-}" \
  --arg chatApiUrl "${FRONTEND_CHAT_API_URL:-http://localhost:8080}" \
  --arg ingestionApiUrl "${FRONTEND_INGESTION_API_URL:-http://localhost:8081}" \
  '{googleClientId: $googleClientId, chatApiUrl: $chatApiUrl, ingestionApiUrl: $ingestionApiUrl, localIdentity: false}' \
  | sed '1s/^/window.HORIZON_CONFIG = /; $s/$/;/' > /usr/share/nginx/html/config.js
