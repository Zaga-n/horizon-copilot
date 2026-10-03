-- Platform/admin command after both migrations. Runtime roles have DML, never DDL.
GRANT SELECT, INSERT, UPDATE ON app.users TO chat_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  app.conversations, app.turns, app.messages, app.agent_runs, app.retry_requests,
  app.message_feedback, app.thread_feedback TO chat_runtime;
GRANT SELECT ON app.alembic_version, app.documents, app.document_versions, app.document_chunks TO chat_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA langgraph TO chat_runtime;
GRANT SELECT (id, subject), INSERT ON app.users TO ingestion_runtime;
GRANT SELECT ON app.alembic_version TO ingestion_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.documents, app.document_versions, app.document_chunks TO ingestion_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.ingestion_jobs, app.upload_requests, app.vendor_permits TO ingestion_runtime;
ALTER ROLE chat_runtime SET search_path = app, public;
ALTER ROLE chat_runtime SET statement_timeout = '10s';
ALTER ROLE chat_runtime SET lock_timeout = '5s';
ALTER ROLE ingestion_runtime SET search_path = app, public;
ALTER ROLE ingestion_runtime SET statement_timeout = '10s';
ALTER ROLE ingestion_runtime SET lock_timeout = '5s';
