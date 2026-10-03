"""End to end over HTTP: cited chat answers from uploads, owner isolation and auth order."""

from uuid import UUID, uuid4

import pytest

from horizon_chat.db.conversations import SqlConversationStore
from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.db.runs import SqlRunLedger
from horizon_chat.domain.agent import AnswerDelta, AnswerSources
from horizon_chat.domain.runs import AttemptStatus, CheckpointStart, RunIdentity
from horizon_ingestion.adapters.identity import LocalIdentityVerifier
from horizon_ingestion.ports.identity import InvalidIdentityError
from horizon_ingestion_testing.chat import cited_agent, quiet_telemetry
from horizon_ingestion_testing.files import pdf_bytes, word_bytes
from horizon_ingestion_testing.pipeline import Harness, upload_pdf

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("filename", "mime", "content"),
    [
        ("a.pdf", "application/pdf", pdf_bytes(pages=("Page one", "Page two"))),
        (
            "a.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            word_bytes(),
        ),
    ],
    ids=["pdf", "word"],
)
async def test_upload_to_compiled_cited_chat_and_history_after_deletion(
    harness: Harness, filename: str, mime: str, content: bytes
) -> None:
    result = await harness.client.post(
        "/v1/documents",
        files={"file": (filename, content, mime)},
        headers={"Idempotency-Key": "cited-chat"},
    )
    assert result.status_code == 202
    claim = await harness.claim()
    await harness.process(claim)
    conversations = SqlConversationStore(engine=harness.engine)
    runs = SqlRunLedger(engine=harness.engine, turn_lease_seconds=150)
    conversation = await conversations.create(subject="owner")
    admission = await runs.admit(
        subject="owner",
        conversation_id=conversation.id,
        request_key="question",
        content="What does the project evidence say?",
        identity=RunIdentity(
            id=uuid4(),
            assistant_message_id=uuid4(),
            trace_id=uuid4().hex,
            root_span_id=uuid4().hex[:16],
            agent_version="test",
            prompt_version="test",
            retrieval_version="test",
        ),
    )
    agent = cited_agent(
        PgvectorEvidenceIndex(
            engine=harness.engine,
            embedding_model_id="amazon.titan-embed-text-v2:0",
            telemetry=quiet_telemetry(),
        )
    )
    events = [
        event
        async for event in agent.answer(
            subject="owner",
            admission=admission,
            start=CheckpointStart(conversation_id=conversation.id, checkpoint_id=None),
        )
    ]
    sources = next(event.sources for event in events if isinstance(event, AnswerSources))
    answer = "".join(event.text for event in events if isinstance(event, AnswerDelta))
    assert answer == "Indexed evidence [S1]."
    assert sources[0].filename == filename
    assert sources[0].document_version_id == claim.version_id
    if filename.endswith(".pdf"):
        assert tuple(locator.page for locator in sources[0].locators) == (1, 2)
    else:
        assert sources[0].section_heading == "Scope"
    await runs.finish(
        subject="owner",
        run_id=admission.run.id,
        status=AttemptStatus.COMPLETED,
        content=answer,
        sources=sources,
        failure_category=None,
    )
    history = await conversations.history(
        subject="owner", conversation_id=conversation.id, cursor=0, limit=10
    )
    assert not history.messages[-1].sources[0].unavailable
    await harness.client.delete(f"/v1/documents/{claim.document_id}")
    await harness.process(await harness.claim())
    history = await conversations.history(
        subject="owner", conversation_id=conversation.id, cursor=0, limit=10
    )
    snapshot = history.messages[-1].sources[0]
    assert snapshot.unavailable
    assert snapshot.model_copy(update={"unavailable": False}) == sources[0]
    assert history.messages[-1].content == answer


async def test_http_owner_isolation_and_authentication_precedes_parsing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verified(self: LocalIdentityVerifier, *, token: str | None) -> str:
        if token not in ("owner", "intruder"):
            raise InvalidIdentityError()
        return token

    monkeypatch.setattr(LocalIdentityVerifier, "verify", verified)
    missing = await harness.client.post(
        "/v1/documents",
        files={"file": ("broken.pdf", b"broken", "application/pdf")},
        headers={"Idempotency-Key": "unauthorized"},
    )
    assert missing.status_code == 401
    harness.client.headers["Authorization"] = "Bearer owner"
    response = await upload_pdf(harness, key="private", text_value="Private evidence")
    assert response.status_code == 202
    accepted = response.json()
    harness.client.headers["Authorization"] = "Bearer intruder"
    assert (await harness.client.get(accepted["status_url"])).status_code == 404
    assert (await harness.client.get(f"/v1/documents/{accepted['document_id']}")).status_code == 404
    assert (await harness.client.get("/v1/documents")).json() == []
    assert (
        await harness.client.delete(f"/v1/documents/{accepted['document_id']}")
    ).status_code == 404
    assert (
        await harness.client.post(f"/v1/ingestion-jobs/{accepted['job_id']}:retry")
    ).status_code == 404
    replacement = await upload_pdf(
        harness,
        key="foreign-target",
        text_value="Foreign replacement",
        document_id=UUID(accepted["document_id"]),
    )
    assert replacement.status_code == 404
