"""Handles to the shared schema's chat tables; every db/ module reads them from here."""

from horizon_schema import metadata

USERS = metadata.tables["app.users"]
CONVERSATIONS = metadata.tables["app.conversations"]
TURNS = metadata.tables["app.turns"]
MESSAGES = metadata.tables["app.messages"]
RUNS = metadata.tables["app.agent_runs"]
RETRIES = metadata.tables["app.retry_requests"]
MESSAGE_FEEDBACK = metadata.tables["app.message_feedback"]
THREAD_FEEDBACK = metadata.tables["app.thread_feedback"]
DOCUMENTS = metadata.tables["app.documents"]
VERSIONS = metadata.tables["app.document_versions"]
CHUNKS = metadata.tables["app.document_chunks"]
