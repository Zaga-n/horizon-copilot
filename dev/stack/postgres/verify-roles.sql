-- Run as admin after migrations and runtime grants. All writes roll back.
\set ON_ERROR_STOP on
BEGIN;
SET ROLE app_migrator;
CREATE TABLE app._role_probe (id integer);
DROP TABLE app._role_probe;
RESET ROLE;
SET ROLE checkpoint_migrator;
CREATE TABLE langgraph._role_probe (id integer);
DROP TABLE langgraph._role_probe;
RESET ROLE;
DO $$
BEGIN
  IF has_schema_privilege('app_migrator', 'langgraph', 'CREATE')
    OR has_schema_privilege('checkpoint_migrator', 'app', 'CREATE')
    OR has_schema_privilege('chat_runtime', 'app', 'CREATE')
    OR has_schema_privilege('chat_runtime', 'langgraph', 'CREATE')
    OR has_schema_privilege('ingestion_runtime', 'app', 'CREATE')
    OR has_schema_privilege('ingestion_runtime', 'langgraph', 'USAGE')
    OR has_table_privilege('ingestion_runtime', 'app.messages', 'SELECT')
    OR has_table_privilege('chat_runtime', 'app.document_chunks', 'INSERT')
    OR has_database_privilege('chat_runtime', 'horizon', 'CREATE')
    OR has_database_privilege('ingestion_runtime', 'horizon', 'CREATE') THEN
    RAISE EXCEPTION 'A restricted role has excessive privileges';
  END IF;
END $$;
SET ROLE chat_runtime;
DO $$ BEGIN
  BEGIN CREATE TABLE app._denied(id integer); RAISE EXCEPTION 'chat DDL allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN INSERT INTO app.document_chunks(id) VALUES (gen_random_uuid()); RAISE EXCEPTION 'chat indexing allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
SELECT count(*) FROM app.messages;
SELECT count(*) FROM app.document_chunks;
SELECT count(*) FROM langgraph.checkpoints;
RESET ROLE;
SET ROLE ingestion_runtime;
DO $$ BEGIN
  BEGIN CREATE TABLE app._denied(id integer); RAISE EXCEPTION 'ingestion DDL allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM app.messages; RAISE EXCEPTION 'ingestion history access allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN PERFORM 1 FROM langgraph.checkpoints; RAISE EXCEPTION 'ingestion checkpoint access allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
END $$;
SELECT count(*) FROM app.ingestion_jobs;
SELECT count(*) FROM app.document_chunks;
RESET ROLE;
ROLLBACK;
