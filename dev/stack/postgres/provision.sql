\set ON_ERROR_STOP on
\i /schema/provision.sql
\getenv app_password APP_MIGRATOR_PASSWORD
\getenv checkpoint_password CHECKPOINT_MIGRATOR_PASSWORD
\getenv chat_password CHAT_DATABASE_PASSWORD
\getenv ingestion_password INGESTION_DATABASE_PASSWORD
ALTER ROLE app_migrator LOGIN PASSWORD :'app_password';
ALTER ROLE checkpoint_migrator LOGIN PASSWORD :'checkpoint_password';
ALTER ROLE chat_runtime LOGIN PASSWORD :'chat_password';
ALTER ROLE ingestion_runtime LOGIN PASSWORD :'ingestion_password';
REVOKE CREATE ON DATABASE horizon FROM PUBLIC;
