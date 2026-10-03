"""Minimal valid deployment settings for tests that build the app or worker wiring."""

from horizon_ingestion.config.settings import Settings


def deployment_settings() -> Settings:
    return Settings(
        environment_name="local",
        bind_host="127.0.0.1",
        bind_port=8081,
        frontend_origin="http://localhost:3000",
        aws_region="eu-west-1",
        embedding_model_id="amazon.titan-embed-text-v2:0",
        google_client_id="public-client",
        minio_endpoint="http://localhost:9000",
        minio_bucket="documents",
    )
