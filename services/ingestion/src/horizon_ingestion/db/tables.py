"""Handles to the shared schema's ingestion tables; every db/ module reads them from here."""

from horizon_schema import metadata

USERS = metadata.tables["app.users"]
DOCUMENTS = metadata.tables["app.documents"]
VERSIONS = metadata.tables["app.document_versions"]
JOBS = metadata.tables["app.ingestion_jobs"]
CHUNKS = metadata.tables["app.document_chunks"]
REQUESTS = metadata.tables["app.upload_requests"]
PERMITS = metadata.tables["app.vendor_permits"]
