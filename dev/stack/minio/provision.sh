#!/bin/sh
# Run with mc installed and a privileged MC_HOST_local exported by the platform.
set -eu
: "${MC_HOST_local:?set the privileged MinIO endpoint}"
: "${INGESTION_MINIO_ACCESS_KEY:?set the ingestion account name}"
: "${INGESTION_MINIO_SECRET_KEY:?set the ingestion account secret}"
policy_path="$(dirname "$0")/ingestion-policy.json"
mc mb --ignore-existing local/horizon-documents
mc version enable local/horizon-documents
mc anonymous set none local/horizon-documents
mc admin user add local "$INGESTION_MINIO_ACCESS_KEY" "$INGESTION_MINIO_SECRET_KEY"
mc admin policy create local horizon-ingestion "$policy_path"
mc admin policy attach local horizon-ingestion --user "$INGESTION_MINIO_ACCESS_KEY"
