"""A package is a list its curator can narrow, and basket files join it only when attached
(ADR 0027).

Four people in one tenant:

* the owner — creates and curates, and holds `kt:manage` and `basket:manage`;
* A, the subject — whose documents and uploads the package carries;
* C, the recipient — who opens it and asks;
* D, a bystander.

A second tenant's owner stands in for every cross-tenant refusal.

Every chunk and every question embed to one direction and `k` covers the whole corpus, so
similarity cannot decide what comes back — only the package boundary can, and a document
that leaks is present in the result rather than ranked out of it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender, get_object_store
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import configure_answers

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}
OTHER_TENANT = {
    "full_name": "Grace Hopper",
    "work_email": "grace@other-tenant.example",
    "company_name": "Other Tenant",
    "company_domain": "other-tenant.example",
    "job_title": "Director",
    "org_size": "51-200",
    "terms_accepted": True,
}

OWNER = "ada@example.com"
SUBJECT = "a.leaver@example.com"
RECIPIENT = "c.recipient@example.com"
BYSTANDER = "d.bystander@example.com"

FULL_SCOPE = ["documents", "profile", "decisions", "people", "projects", "meetings"]
VECTOR = [1.0] + [0.0] * 767
VECTOR_LITERAL = "[" + ",".join(repr(value) for value in VECTOR) + "]"
K = 100

#: What the package holds before anybody curates it: A's own connector documents inside the
#: period. Not A's archive, not the document written after the period ended, not C's own
#: document, and none of A's basket uploads — none is attached yet.
UNCURATED = {"A_KEEP", "A_PRIVATE"}


class AlignedEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return list(VECTOR), 5


class CitingAnswers:
    """Cites every numbered passage the prompt carries, so every source becomes a citation."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        numbers = sorted({int(n) for n in re.findall(r"^\[(\d{1,3})\] ", prompt, flags=re.M)})
        if not numbers:
            return "INSUFFICIENT_EVIDENCE"
        return "Grounded " + "".join(f"[{n}]" for n in numbers) + "."


class FakeStore:
    """Only what deleting a basket file touches."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def signed_download(self, key: str, *, filename: str) -> str:
        return f"https://storage.example/{key}"

    def delete(self, key: str) -> None:
        self.deleted.append(key)


@dataclass
class World:
    http: AsyncClient
    answers: CitingAnswers
    db: AsyncSession
    mailbox: RecordingEmailSender


@dataclass
class Seeded:
    code: str
    package_id: str
    org_id: str
    ids: dict[str, str]
    documents: dict[str, str] = field(default_factory=dict)
    chunks: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)


@pytest.fixture
async def world(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[World]:
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)
    configure_answers(monkeypatch)
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    answers = CitingAnswers()
    store = FakeStore()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_query_embedder] = lambda: AlignedEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: answers
    app.dependency_overrides[get_object_store] = lambda: store

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as http:
        yield World(http=http, answers=answers, db=db_session, mailbox=mailbox)

    await dispose_engine()


# ---------------------------------------------------------------------------- helpers


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


def keys(items: list[dict[str, Any]], column: str = "document_title") -> set[str]:
    """Every fixture title starts with its key: `A_PRIVATE personal leave request`."""
    return {str(item[column]).split(" ", 1)[0] for item in items}


async def register(world: World, form: dict[str, Any]) -> None:
    await world.http.post("/v1/orgs/register", json=form)
    delivered = world.mailbox.last.secrets
    verified = await world.http.post(
        "/v1/orgs/register/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def sign_in(world: World, email: str) -> None:
    await world.http.post("/v1/auth/request", json={"email": email})
    delivered = world.mailbox.last.secrets
    verified = await world.http.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def join(world: World, *emails: str) -> None:
    """Invite everybody while the owner is signed in, then accept each invitation."""
    tokens: list[str] = []
    for email in emails:
        invited = await world.http.post(
            "/v1/employees/invitations",
            json={"email": email, "role": "member"},
            headers=csrf(world.http),
        )
        assert invited.status_code == 202, invited.text
        tokens.append(world.mailbox.last.secrets["token"])
    for email, token in zip(emails, tokens, strict=True):
        accepted = await world.http.post(
            "/v1/invitations/accept",
            json={"token": token, "full_name": email.split("@")[0].title()},
        )
        assert accepted.status_code == 200, accepted.text


async def user_id_of(world: World, email: str) -> str:
    page = (await world.http.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def ask(world: World, code: str, question: str = "What did A leave behind?") -> Response:
    return await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )


async def as_owner(world: World) -> None:
    await sign_in(world, OWNER)


async def as_recipient(world: World) -> None:
    await sign_in(world, RECIPIENT)


async def _document(
    db: AsyncSession,
    seeded: Seeded,
    *,
    key: str,
    title: str,
    source_id: uuid.UUID,
    external_id: str,
    principal: str,
    age_days: float,
    claim: tuple[str, str] | None = None,
) -> None:
    document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    passage = f"{key}: {title}."
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, :title, :hash, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day')"
        ),
        {
            "id": document_id,
            "org": seeded.org_id,
            "src": source_id,
            "ext": external_id,
            "title": title,
            "hash": key.lower(),
            "body": passage,
            "age": age_days,
        },
    )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": document_id, "pid": principal, "org": seeded.org_id},
    )
    await db.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) VALUES (:id, :doc, :org, 0, :text, 0, :end, 8, "
            "CAST(:vec AS vector))"
        ),
        {
            "id": chunk_id,
            "doc": document_id,
            "org": seeded.org_id,
            "text": passage,
            "end": len(passage),
            "vec": VECTOR_LITERAL,
        },
    )
    if claim is not None:
        run_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "
                "finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', now(), "
                "cast(:stats AS jsonb))"
            ),
            {
                "id": run_id,
                "org": seeded.org_id,
                "stats": json.dumps({"document_id": str(document_id)}),
            },
        )
        claim_type, name = claim
        await db.execute(
            text(
                "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                "payload_json, confidence) VALUES (gen_random_uuid(), :run, :chunk, :org, "
                ":type, cast(:payload AS jsonb), 0.9)"
            ),
            {
                "run": run_id,
                "chunk": chunk_id,
                "org": seeded.org_id,
                "type": claim_type,
                "payload": json.dumps({"name": name, "summary": name, "quote": passage}),
            },
        )
    seeded.documents[key] = str(document_id)
    seeded.chunks[key] = str(chunk_id)


async def seed(world: World, seeded: Seeded) -> None:
    """A's documents, A's basket uploads (ingested, none attached) and C's own document."""
    db = world.db
    subject_id = seeded.ids["A"]
    await db.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": seeded.org_id}
    )
    local, basket = uuid.uuid4(), uuid.uuid4()
    for source_id, system in ((local, "local"), (basket, "basket")):
        await db.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, CAST(:system AS source_system), '{}'::jsonb)"
            ),
            {"id": source_id, "org": seeded.org_id, "system": system},
        )
    # The principal an upload links for its owner (`identities.link_basket_principal`).
    await db.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject, linked_by) "
            "VALUES (:org, :user, CAST('basket' AS source_system), :subject, 'basket_upload')"
        ),
        {"org": seeded.org_id, "user": subject_id, "subject": subject_id},
    )

    subject_local, recipient_local = f"local:{SUBJECT}", f"local:{RECIPIENT}"
    for key, title, principal, age, claim in (
        ("A_KEEP", "A_KEEP handover plan", subject_local, 5, ("decision", "A_KEEP decision")),
        ("A_PRIVATE", "A_PRIVATE leave request", subject_local, 3, ("person", "A_PRIVATE doctor")),
        ("A_OLD", "A_OLD archive", subject_local, 400, None),
        # Written after the package was created, so after its period ended.
        ("A_LATE", "A_LATE follow-up", subject_local, -0.01, None),
        ("C_OWN", "C_OWN own notes", recipient_local, 2, None),
    ):
        await _document(
            db,
            seeded,
            key=key,
            title=title,
            source_id=local,
            external_id=key.lower(),
            principal=principal,
            age_days=age,
            claim=claim,
        )

    for key, age in (("BASKET_SHARED", 1), ("BASKET_PRIVATE", 1), ("BASKET_LATE", -0.01)):
        file_id = uuid.uuid4()
        await _document(
            db,
            seeded,
            key=key,
            title=f"{key} upload.txt",
            source_id=basket,
            external_id=str(file_id),
            principal=f"basket:{subject_id}",
            age_days=age,
        )
        await db.execute(
            text(
                "INSERT INTO basket_files (id, org_id, owner_user_id, object_key, "
                "original_filename, normalised_filename, declared_mime, size_bytes, state, "
                "document_id, extracted_chars) VALUES (:id, :org, :owner, :key, :name, :norm, "
                "'text/plain', 64, 'ready', :doc, 64)"
            ),
            {
                "id": file_id,
                "org": seeded.org_id,
                "owner": subject_id,
                "key": f"org/{seeded.org_id}/basket/{file_id}",
                "name": f"{key} upload.txt",
                "norm": f"{key.lower()} upload.txt",
                "doc": uuid.UUID(seeded.documents[key]),
            },
        )
        seeded.files[key] = str(file_id)
    await db.commit()


async def build(world: World) -> Seeded:
    """The tenant, A's package addressed to C and claimed by C, and C left signed in."""
    await register(world, REGISTRATION)
    await join(world, SUBJECT, RECIPIENT, BYSTANDER)
    await as_owner(world)
    ids = {
        "A": await user_id_of(world, SUBJECT),
        "C": await user_id_of(world, RECIPIENT),
        "D": await user_id_of(world, BYSTANDER),
    }
    org_id = str((await world.http.get("/v1/orgs/current")).json()["id"])
    created = await world.http.post(
        "/v1/kt",
        json={
            "subject_user_id": ids["A"],
            "scope": FULL_SCOPE,
            "validity_days": 30,
            "period_days": 90,
            "recipient_email": RECIPIENT,
        },
        headers=csrf(world.http),
    )
    assert created.status_code == 201, created.text
    package = created.json()
    seeded = Seeded(
        code=str(package["kt_code"]), package_id=str(package["id"]), org_id=org_id, ids=ids
    )
    await seed(world, seeded)

    await as_recipient(world)
    claimed = await world.http.post(
        "/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(world.http)
    )
    assert claimed.status_code == 200, claimed.text
    return seeded


async def attach(world: World, seeded: Seeded, *file_keys: str) -> None:
    await as_owner(world)
    attached = await world.http.post(
        f"/v1/kt/{seeded.package_id}/attachments",
        json={"file_ids": [seeded.files[key] for key in file_keys]},
        headers=csrf(world.http),
    )
    assert attached.status_code == 201, attached.text
    assert attached.json()["attached"] == len(file_keys)
    await as_recipient(world)


async def exclude(world: World, seeded: Seeded, key: str) -> None:
    await as_owner(world)
    excluded = await world.http.post(
        f"/v1/kt/{seeded.package_id}/exclusions",
        json={"document_id": seeded.documents[key]},
        headers=csrf(world.http),
    )
    assert excluded.status_code == 201, excluded.text
    assert excluded.json()["excluded"] is True
    await as_recipient(world)


async def sources_for(world: World, seeded: Seeded) -> set[str]:
    response = await ask(world, seeded.code)
    assert response.status_code == 200, response.text
    return keys(response.json()["sources"])


async def listed_for(world: World, seeded: Seeded) -> set[str]:
    response = await world.http.get(f"/v1/kt/{seeded.code}/documents", params={"limit": 100})
    assert response.status_code == 200, response.text
    return keys(response.json()["items"], "title")


# ---------------------------------------------------------------- basket files join by attachment


class TestBasketFilesJoinByAttachment:
    async def test_an_uploaded_file_is_not_in_the_package_until_it_is_attached(
        self, world: World
    ) -> None:
        seeded = await build(world)

        assert await sources_for(world, seeded) == UNCURATED
        assert await listed_for(world, seeded) == UNCURATED
        for key in ("BASKET_SHARED", "BASKET_PRIVATE"):
            span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks[key]}")
            assert span.status_code == 404, f"{key} opened through the KT door unattached"

    async def test_an_attached_file_is_searched_cited_and_listed(self, world: World) -> None:
        seeded = await build(world)
        await attach(world, seeded, "BASKET_SHARED")

        body = (await ask(world, seeded.code)).json()
        span = await world.http.get(
            f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['BASKET_SHARED']}"
        )

        assert keys(body["sources"]) == UNCURATED | {"BASKET_SHARED"}
        assert seeded.documents["BASKET_SHARED"] in {c["document_id"] for c in body["citations"]}
        assert span.status_code == 200, span.text
        assert await listed_for(world, seeded) == UNCURATED | {"BASKET_SHARED"}

    async def test_a_file_attached_after_the_period_ended_still_travels(self, world: World) -> None:
        """An attachment is an explicit choice; the period binds connector documents."""
        seeded = await build(world)
        await attach(world, seeded, "BASKET_LATE")

        sources = await sources_for(world, seeded)

        assert "BASKET_LATE" in sources
        assert "A_LATE" not in sources, "the period no longer binds connector documents"

    async def test_detaching_or_deleting_a_file_removes_it_on_the_next_request(
        self, world: World
    ) -> None:
        seeded = await build(world)
        await attach(world, seeded, "BASKET_SHARED", "BASKET_PRIVATE")
        assert await sources_for(world, seeded) == UNCURATED | {"BASKET_SHARED", "BASKET_PRIVATE"}

        await as_owner(world)
        detached = await world.http.delete(
            f"/v1/kt/{seeded.package_id}/attachments/{seeded.files['BASKET_SHARED']}",
            headers=csrf(world.http),
        )
        deleted = await world.http.delete(
            f"/v1/basket/files/{seeded.files['BASKET_PRIVATE']}", headers=csrf(world.http)
        )
        assert detached.status_code == 204, detached.text
        assert deleted.status_code in (200, 204), deleted.text
        await as_recipient(world)

        assert await sources_for(world, seeded) == UNCURATED
        for key in ("BASKET_SHARED", "BASKET_PRIVATE"):
            span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks[key]}")
            assert span.status_code == 404


# ------------------------------------------------------------------ the handover report


class TestTheHandoverReport:
    async def test_it_prints_the_packages_files_and_folders_and_nothing_else(
        self, world: World
    ) -> None:
        """The PDF reads through the scope Ask KT reads through (ADR 0027, ADR 0029): an
        attached file is listed with whether Ask KT can search it, a kept-back or unattached
        one is not, and a folder is named only through a document the package carries."""
        import base64
        from io import BytesIO

        from pypdf import PdfReader

        seeded = await build(world)
        await attach(world, seeded, "BASKET_SHARED", "BASKET_PRIVATE")
        await exclude(world, seeded, "BASKET_PRIVATE")
        await world.db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": seeded.org_id}
        )
        for key, folder in (
            ("A_KEEP", "Projects/Astro Agent"),
            ("A_PRIVATE", "Personal/Leave"),
            ("A_OLD", "Archive/Old Projects"),
            ("C_OWN", "Recipient/Own Notes"),
        ):
            await world.db.execute(
                text("UPDATE documents SET folder_path = :folder WHERE id = :id"),
                {"folder": folder, "id": seeded.documents[key]},
            )
        await world.db.commit()
        await exclude(world, seeded, "A_PRIVATE")

        response = await world.http.post(
            f"/v1/kt/{seeded.code}/handover-report", headers=csrf(world.http)
        )

        assert response.status_code == 200, response.text
        reader = PdfReader(BytesIO(base64.b64decode(response.json()["pdf_base64"])))
        printed = " ".join(" ".join(page.extract_text() for page in reader.pages).split())
        assert "Files shared with this package" in printed
        assert "BASKET_SHARED upload.txt · Searchable" in printed
        assert "BASKET_PRIVATE" not in printed, "a kept-back file was printed"
        assert "BASKET_LATE" not in printed, "an unattached file was printed"
        assert "Where documents are kept" in printed
        assert "Projects/Astro Agent · 1 document" in printed
        for outside in ("Personal/Leave", "Archive/Old Projects", "Recipient/Own Notes"):
            assert outside not in printed, f"{outside} was printed"


# ------------------------------------------------------------------------- exclusions


class TestExclusions:
    async def test_a_kept_back_file_leaves_the_files_tab_and_its_download(
        self, world: World
    ) -> None:
        """ADR 0027 for a basket file: keeping its document back withdraws the file itself,
        exactly as detaching it would, behind the same 404."""
        seeded = await build(world)
        await attach(world, seeded, "BASKET_SHARED", "BASKET_PRIVATE")

        await exclude(world, seeded, "BASKET_PRIVATE")

        listing = await world.http.get(f"/v1/kt/{seeded.code}/files")
        assert listing.status_code == 200, listing.text
        assert [f["filename"] for f in listing.json()["items"]] == ["BASKET_SHARED upload.txt"]
        kept = await world.http.get(
            f"/v1/kt/{seeded.code}/files/{seeded.files['BASKET_PRIVATE']}/download"
        )
        shared = await world.http.get(
            f"/v1/kt/{seeded.code}/files/{seeded.files['BASKET_SHARED']}/download"
        )
        assert kept.status_code == 404, kept.text
        assert shared.status_code == 200, shared.text

    async def test_an_excluded_document_leaves_every_read_a_recipient_has(
        self, world: World
    ) -> None:
        seeded = await build(world)
        before = (await ask(world, seeded.code)).json()
        assert keys(before["sources"]) == UNCURATED

        await exclude(world, seeded, "A_PRIVATE")

        assert await sources_for(world, seeded) == {"A_KEEP"}
        assert await listed_for(world, seeded) == {"A_KEEP"}
        reader = await world.http.get(
            f"/v1/kt/{seeded.code}/documents/{seeded.documents['A_PRIVATE']}"
        )
        span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['A_PRIVATE']}")
        insights = (await world.http.get(f"/v1/kt/{seeded.code}/insights")).json()["items"]
        counts = (await world.http.get(f"/v1/kt/{seeded.code}/insights-summary")).json()
        workspace = (await world.http.get(f"/v1/kt/{seeded.code}/workspace")).json()
        replay = (
            await world.http.get(f"/v1/kt/{seeded.code}/conversations/{before['conversation_id']}")
        ).json()

        assert reader.status_code == 404
        assert span.status_code == 404
        assert {item["name"] for item in insights} == {"A_KEEP decision"}
        assert counts["by_type"] == {"decision": 1}
        assert workspace["coverage"]["documents_visible"] == 1
        available = {
            c["document_id"]: c["available"] for m in replay["messages"] for c in m["citations"]
        }
        assert available[seeded.documents["A_PRIVATE"]] is False
        assert available[seeded.documents["A_KEEP"]] is True

        world.answers.prompts.clear()
        summary = await world.http.get(f"/v1/kt/{seeded.code}/handover-summary")
        assert summary.status_code == 200, summary.text
        assert world.answers.prompts and "A_PRIVATE" not in world.answers.prompts[-1]

    async def test_putting_a_document_back_restores_it(self, world: World) -> None:
        seeded = await build(world)
        await exclude(world, seeded, "A_PRIVATE")

        await as_owner(world)
        restored = await world.http.delete(
            f"/v1/kt/{seeded.package_id}/exclusions/{seeded.documents['A_PRIVATE']}",
            headers=csrf(world.http),
        )
        again = await world.http.delete(
            f"/v1/kt/{seeded.package_id}/exclusions/{seeded.documents['A_PRIVATE']}",
            headers=csrf(world.http),
        )
        await as_recipient(world)

        assert restored.status_code == 204, restored.text
        assert again.status_code == 404, "restoring twice is not a second change"
        assert await sources_for(world, seeded) == UNCURATED

    async def test_an_exclusion_survives_a_new_version_of_the_document(self, world: World) -> None:
        """Keyed by source and external id: a re-sync must not bring the document back."""
        seeded = await build(world)
        await exclude(world, seeded, "A_PRIVATE")

        db = world.db
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": seeded.org_id}
        )
        old = (
            await db.execute(
                text("SELECT source_id, external_id FROM documents WHERE id = :id"),
                {"id": seeded.documents["A_PRIVATE"]},
            )
        ).one()
        new_id, chunk_id = uuid.uuid4(), uuid.uuid4()
        await db.execute(text("SET CONSTRAINTS fk_documents_superseded_by DEFERRED"))
        await db.execute(
            text("UPDATE documents SET superseded_by = :new WHERE id = :old"),
            {"new": new_id, "old": seeded.documents["A_PRIVATE"]},
        )
        await db.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
                "acl_hash, body_original, body_masked, created_at) VALUES (:id, :org, :src, "
                ":ext, 'A_PRIVATE leave request, revised', 'v2', 'a', 'A_PRIVATE: revised.', "
                "'A_PRIVATE: revised.', now() - interval '1 day')"
            ),
            {"id": new_id, "org": seeded.org_id, "src": old.source_id, "ext": old.external_id},
        )
        await db.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
                "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
            ),
            {"doc": new_id, "pid": f"local:{SUBJECT}", "org": seeded.org_id},
        )
        await db.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, "
                "char_end, token_count, embedding) VALUES (:id, :doc, :org, 0, "
                "'A_PRIVATE: revised.', 0, 19, 4, CAST(:vec AS vector))"
            ),
            {"id": chunk_id, "doc": new_id, "org": seeded.org_id, "vec": VECTOR_LITERAL},
        )
        await db.commit()

        try:
            assert await sources_for(world, seeded) == {"A_KEEP"}
            await as_owner(world)
            contents = (await world.http.get(f"/v1/kt/{seeded.package_id}/contents")).json()
            revised = [item for item in contents["items"] if item["document_id"] == str(new_id)]
            assert revised and revised[0]["excluded"] is True
        finally:
            # The fixture migrates down after every test, and migration 0010's downgrade
            # refuses two versions of one identifier — the guard CLAUDE.md records. The
            # superseded version therefore goes before the downgrade does.
            await db.rollback()
            await db.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": seeded.org_id},
            )
            await db.execute(
                text("DELETE FROM documents WHERE id = :old"),
                {"old": seeded.documents["A_PRIVATE"]},
            )
            await db.commit()


class TestWhoCurates:
    async def test_only_the_owner_and_the_subject_may_review_or_exclude(self, world: World) -> None:
        seeded = await build(world)
        target = {"document_id": seeded.documents["A_PRIVATE"]}

        outcomes: dict[str, tuple[int, int]] = {}
        for label, email in (("C", RECIPIENT), ("D", BYSTANDER)):
            await sign_in(world, email)
            review = await world.http.get(f"/v1/kt/{seeded.package_id}/contents")
            write = await world.http.post(
                f"/v1/kt/{seeded.package_id}/exclusions", json=target, headers=csrf(world.http)
            )
            outcomes[label] = (review.status_code, write.status_code)
        await sign_in(world, SUBJECT)
        own_review = await world.http.get(f"/v1/kt/{seeded.package_id}/contents")
        own_write = await world.http.post(
            f"/v1/kt/{seeded.package_id}/exclusions", json=target, headers=csrf(world.http)
        )

        assert outcomes == {"C": (404, 404), "D": (404, 404)}
        assert (own_review.status_code, own_write.status_code) == (200, 201)

        await register(world, OTHER_TENANT)
        foreign_review = await world.http.get(f"/v1/kt/{seeded.package_id}/contents")
        foreign_write = await world.http.post(
            f"/v1/kt/{seeded.package_id}/exclusions", json=target, headers=csrf(world.http)
        )
        assert (foreign_review.status_code, foreign_write.status_code) == (404, 404)

    async def test_a_review_shows_titles_and_flags_never_a_passage(
        self, world: World, inspector: AsyncSession
    ) -> None:
        seeded = await build(world)
        await attach(world, seeded, "BASKET_SHARED")
        await exclude(world, seeded, "A_PRIVATE")

        await as_owner(world)
        review = await world.http.get(f"/v1/kt/{seeded.package_id}/contents")

        assert review.status_code == 200, review.text
        by_key = {item["title"].split(" ", 1)[0]: item for item in review.json()["items"]}
        assert set(by_key) == UNCURATED | {"BASKET_SHARED"}
        assert by_key["A_PRIVATE"]["excluded"] is True
        assert by_key["A_KEEP"]["excluded"] is False
        assert by_key["BASKET_SHARED"]["attached_file"] is True
        for key in by_key:
            assert f"{key}:" not in review.text, "a passage reached the curator's review"

        rows = (
            await inspector.execute(
                text(
                    "SELECT action, meta_json FROM audit_log "
                    "WHERE action IN ('kt.contents_reviewed', 'kt.document_excluded')"
                )
            )
        ).all()
        assert {row.action for row in rows} == {"kt.contents_reviewed", "kt.document_excluded"}
        for row in rows:
            assert "A_PRIVATE" not in json.dumps(row.meta_json), "a title reached the trail"

    async def test_nothing_outside_the_package_can_be_excluded(
        self, world: World, inspector: AsyncSession
    ) -> None:
        seeded = await build(world)

        await as_owner(world)
        refusals = [
            (
                await world.http.post(
                    f"/v1/kt/{seeded.package_id}/exclusions",
                    json={"document_id": document_id},
                    headers=csrf(world.http),
                )
            ).status_code
            for document_id in (
                seeded.documents["C_OWN"],
                seeded.documents["A_OLD"],
                seeded.documents["BASKET_PRIVATE"],
                str(uuid.uuid4()),
            )
        ]

        assert refusals == [404, 404, 404, 404]
        count = (
            await inspector.execute(text("SELECT count(*) FROM kt_package_exclusions"))
        ).scalar_one()
        assert count == 0

    async def test_a_closed_package_allows_withdrawal_and_refuses_putting_back(
        self, world: World
    ) -> None:
        seeded = await build(world)

        await as_owner(world)
        revoked = await world.http.post(
            f"/v1/kt/{seeded.package_id}/revoke", headers=csrf(world.http)
        )
        withdrawn = await world.http.post(
            f"/v1/kt/{seeded.package_id}/exclusions",
            json={"document_id": seeded.documents["A_PRIVATE"]},
            headers=csrf(world.http),
        )
        restored = await world.http.delete(
            f"/v1/kt/{seeded.package_id}/exclusions/{seeded.documents['A_PRIVATE']}",
            headers=csrf(world.http),
        )

        assert revoked.status_code == 200, revoked.text
        assert (withdrawn.status_code, restored.status_code) == (201, 409)


class TestTheExclusionTable:
    async def test_it_is_tenant_isolated_and_never_edited_in_place(
        self, world: World, inspector: AsyncSession
    ) -> None:
        rls = (
            await inspector.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'kt_package_exclusions'"
                )
            )
        ).one()
        grants = (
            await inspector.execute(
                text(
                    "SELECT has_table_privilege('jutsu_app', 'kt_package_exclusions', 'INSERT') "
                    "AS can_insert, has_table_privilege('jutsu_app', 'kt_package_exclusions', "
                    "'UPDATE') AS can_update"
                )
            )
        ).one()

        assert rls.relrowsecurity and rls.relforcerowsecurity
        assert grants.can_insert and not grants.can_update


class TestWholeHistory:
    async def test_it_must_be_stated_and_cannot_accompany_a_period(self, world: World) -> None:
        await register(world, REGISTRATION)
        await join(world, SUBJECT)
        await as_owner(world)
        subject = await user_id_of(world, SUBJECT)
        base = {"subject_user_id": subject, "scope": ["documents"], "validity_days": 30}

        silent = await world.http.post("/v1/kt", json=base, headers=csrf(world.http))
        both = await world.http.post(
            "/v1/kt",
            json={**base, "period_days": 90, "whole_history": True},
            headers=csrf(world.http),
        )
        stated = await world.http.post(
            "/v1/kt", json={**base, "whole_history": True}, headers=csrf(world.http)
        )
        dated = await world.http.post(
            "/v1/kt", json={**base, "period_days": 30}, headers=csrf(world.http)
        )

        assert silent.status_code == 422
        assert "whole history" in silent.json()["error"]["message"]
        assert both.status_code == 422
        assert stated.status_code == 201, stated.text
        assert stated.json()["period_start"] is None
        assert dated.status_code == 201, dated.text
        assert dated.json()["period_start"] is not None
