-- Platform/admin command before app and checkpoint migrations. No passwords are stored here.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_migrator') THEN
    CREATE ROLE app_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'checkpoint_migrator') THEN
    CREATE ROLE checkpoint_migrator NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'chat_runtime') THEN
    CREATE ROLE chat_runtime NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ingestion_runtime') THEN
    CREATE ROLE ingestion_runtime NOLOGIN;
  END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS langgraph AUTHORIZATION checkpoint_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA app, langgraph FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO chat_runtime, ingestion_runtime;
GRANT USAGE ON SCHEMA langgraph TO chat_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA app REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE checkpoint_migrator IN SCHEMA langgraph REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE checkpoint_migrator IN SCHEMA langgraph
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO chat_runtime;
-- A narrowly scoped admin helper allows schema creation without granting the
-- checkpoint migrator permission to create arbitrary schemas in the database.
CREATE OR REPLACE FUNCTION public.horizon_setup_checkpoint_schema() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
  CREATE SCHEMA IF NOT EXISTS langgraph AUTHORIZATION checkpoint_migrator;
END $$;
REVOKE ALL ON FUNCTION public.horizon_setup_checkpoint_schema() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.horizon_setup_checkpoint_schema() TO checkpoint_migrator;
