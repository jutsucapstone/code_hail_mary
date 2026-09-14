"""A knowledge-transfer package reads its SUBJECT's knowledge — and nothing else (ADR 0025).

Three people, kept apart on purpose:

* **A**, the subject — `leaver@example.com`, whose knowledge the package carries;
* **B**, the requester — `newhire@example.com`, who claimed the package and asks;
* **C**, a colleague — `colleague@example.com`, who holds neither role.

The corpus is built so that every wrong answer is visible. B has a private project of
their own (Zephyr); A has one (Atlas); they share a roadmap; A belongs to a group and to
the organisation, both of which carry documents; and A has an archive older than the
package's window. A correct KT read returns exactly Atlas and the shared roadmap. Any
leak — B's own corpus, A's group or organisation reach, C's notes, the archive, another
tenant — is a named document that turns up where it must not.

Every chunk and every question embed to the same direction, so similarity cannot decide
what comes back. Only authorization can, which is the thing under test.

Against real Postgres and RLS, because a Python post-filter produces identical outcomes
until a count, a LIMIT or a cursor is involved (CLAUDE.md). The last class asserts the
provenance of the scope by AST: that is the half of the design a behavioural test cannot
see, because a second construction site would pass every test here until it was used.
"""

from __future__ import annotations

import ast
import base64
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_core.logs import JsonFormatter
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import configure_answers, unconfigure_answers

API_SRC = Path(__file__).resolve().parents[1] / "src" / "jutsu_api"

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

OWNER_EMAIL = "ada@example.com"
SUBJECT_EMAIL = "leaver@example.com"
RECIPIENT_EMAIL = "newhire@example.com"
COLLEAGUE_EMAIL = "colleague@example.com"
SUBJECT = f"local:{SUBJECT_EMAIL}"
RECIPIENT = f"local:{RECIPIENT_EMAIL}"
COLLEAGUE = f"local:{COLLEAGUE_EMAIL}"
SUBJECT_GROUP = "google:engineering@example.com"

FULL_SCOPE = [
    "documents",
    "profile",
    "decisions",
    "people",
    "projects",
    "meetings",
    "responsibilities",
]

#: One direction in embedding space. Every chunk and every question embed to it, so
#: similarity ties everywhere and only authorization decides what is returned.
VECTOR = [1.0] + [0.0] * 767
VECTOR_LITERAL = "[" + ",".join(repr(value) for value in VECTOR) + "]"

#: What a correct KT read returns: the subject's own documents inside the window.
IN_PACKAGE = {"Atlas migration plan", "Shared roadmap"}
#: Everything else in the corpus, each a distinct way to leak.
NEVER_IN_PACKAGE = {
    "Zephyr launch notes",  # the recipient's own corpus
    "Colleague private notes",  # a third person's
    "Engineering all-hands",  # the subject's GROUP reach, not the subject's own
    "Company handbook",  # the subject's ORG reach
    "Atlas archive 2019",  # the subject's own, but outside the package window
}


class AlignedEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return list(VECTOR), 5


class ScriptedAnswers:
    """Records every prompt; answers with a citation to the first passage unless told."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.replies: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "Grounded in the first passage [1]."


@dataclass
class World:
    http: AsyncClient
    scripted: ScriptedAnswers
    db: AsyncSession
    mailbox: RecordingEmailSender


@dataclass
class Seeded:
    code: str
    package_id: str
    org_id: str
    documents: dict[str, str]
    chunks: dict[str, str]


@pytest.fixture
async def world(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[World]:
    """The app against real Postgres, with the two paid seams scripted.

    The same shape as `test_kt.py`'s client: denied opens and budget spends commit on
    `jutsu_db.engine`'s own engine, so `DATABASE_URL` must be the application role and the
    cached engine disposed around every test.
    """
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)
    configure_answers(monkeypatch)

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    scripted = ScriptedAnswers()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_query_embedder] = lambda: AlignedEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: scripted

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as http:
        yield World(http=http, scripted=scripted, db=db_session, mailbox=mailbox)

    await dispose_engine()


# ---------------------------------------------------------------------------- helpers


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def register(
    client: AsyncClient, mailbox: RecordingEmailSender, form: dict[str, Any]
) -> None:
    await client.post("/v1/orgs/register", json=form)
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text


async def invite_and_accept(
    client: AsyncClient, mailbox: RecordingEmailSender, *, email: str
) -> None:
    invited = await client.post(
        "/v1/employees/invitations", json={"email": email, "role": "member"}, headers=csrf(client)
    )
    assert invited.status_code == 202, invited.text
    accepted = await client.post(
        "/v1/invitations/accept",
        json={"token": mailbox.last.secrets["token"], "full_name": email.split("@")[0].title()},
    )
    assert accepted.status_code == 200, accepted.text


async def sign_in(client: AsyncClient, mailbox: RecordingEmailSender, *, email: str) -> None:
    await client.post("/v1/auth/request", json={"email": email})
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def user_id_of(client: AsyncClient, email: str) -> str:
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def seed_corpus(
    db: AsyncSession, *, org_id: str, subject_id: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Seven documents, each a way to be right or a distinct way to leak."""
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})
    source_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'local', '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id},
    )
    # The subject belongs to a group, so a group-arm leak has something to leak.
    await db.execute(
        text(
            "INSERT INTO user_groups (user_id, group_external_id, org_id) "
            "VALUES (:user, :group, :org)"
        ),
        {"user": subject_id, "group": SUBJECT_GROUP, "org": org_id},
    )

    corpus: tuple[
        tuple[str, str, float, tuple[tuple[str, str], ...], str, tuple[str, str] | None], ...
    ] = (
        (
            "atlas",
            "Atlas migration plan",
            1,
            (("user", SUBJECT),),
            "Atlas moves the ledger to PostgreSQL, owned by the leaver.",
            ("project", "Atlas"),
        ),
        (
            "shared",
            "Shared roadmap",
            2,
            (("user", SUBJECT), ("user", RECIPIENT)),
            "The roadmap ships Atlas before the audit.",
            None,
        ),
        (
            "zephyr",
            "Zephyr launch notes",
            1,
            (("user", RECIPIENT),),
            "Zephyr is the newhire's own launch.",
            ("project", "Zephyr"),
        ),
        (
            "colleague",
            "Colleague private notes",
            1,
            (("user", COLLEAGUE),),
            "Colleague notes, unrelated to any handover.",
            ("decision", "Colleague decision"),
        ),
        (
            "group",
            "Engineering all-hands",
            1,
            (("group", SUBJECT_GROUP),),
            "The whole team decided to freeze deploys.",
            ("decision", "Freeze deploys"),
        ),
        (
            "org",
            "Company handbook",
            1,
            (("org", org_id),),
            "Expenses are approved by finance.",
            ("decision", "Finance approves expenses"),
        ),
        (
            "archive",
            "Atlas archive 2019",
            400,
            (("user", SUBJECT),),
            "The 2019 plan that was abandoned.",
            ("project", "Atlas 2019"),
        ),
    )

    documents: dict[str, str] = {}
    chunks: dict[str, str] = {}
    for key, title, age_days, grants, passage, claim in corpus:
        document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
        documents[key], chunks[key] = str(document_id), str(chunk_id)
        await db.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, "
                "content_hash, acl_hash, body_original, body_masked, created_at) "
                "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :body, :body, "
                "now() - CAST(:age AS double precision) * interval '1 day')"
            ),
            {
                "id": document_id,
                "org": org_id,
                "src": source_id,
                "ext": key,
                "title": title,
                "body": passage,
                "age": age_days,
            },
        )
        for principal_type, principal_id in grants:
            await db.execute(
                text(
                    "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                    "org_id, permission) VALUES (:doc, :type, :pid, :org, 'read')"
                ),
                {"doc": document_id, "type": principal_type, "pid": principal_id, "org": org_id},
            )
        await db.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, "
                "char_end, token_count, embedding) "
                "VALUES (:id, :doc, :org, 0, :text, 0, :end, 8, CAST(:vec AS vector))"
            ),
            {
                "id": chunk_id,
                "doc": document_id,
                "org": org_id,
                "text": passage,
                "end": len(passage),
                "vec": VECTOR_LITERAL,
            },
        )
        if claim is not None:
            claim_type, name = claim
            run_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, "
                    "model, finished_at, stats_json) "
                    "VALUES (:id, :org, 'v1', 'h', 'test', now(), cast(:stats AS jsonb))"
                ),
                {
                    "id": run_id,
                    "org": org_id,
                    "stats": '{"document_id": "' + str(document_id) + '"}',
                },
            )
            await db.execute(
                text(
                    "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                    "payload_json, confidence) "
                    "VALUES (gen_random_uuid(), :run, :chunk, :org, :type, "
                    "cast(:payload AS jsonb), 0.9)"
                ),
                {
                    "run": run_id,
                    "chunk": chunk_id,
                    "org": org_id,
                    "type": claim_type,
                    "payload": (
                        '{"name": "' + name + '", "summary": "' + name + '", '
                        '"quote": "' + passage + '", "document_id": "' + str(document_id) + '"}'
                    ),
                },
            )
    await db.commit()
    return documents, chunks


async def build(world: World, *, scope: list[str] | None = None, period_days: int = 90) -> Seeded:
    """Owner, A, B and C; a package about A, claimed by B; B left signed in."""
    client, mailbox = world.http, world.mailbox
    await register(client, mailbox, REGISTRATION)
    for email in (SUBJECT_EMAIL, RECIPIENT_EMAIL, COLLEAGUE_EMAIL):
        await invite_and_accept(client, mailbox, email=email)
        await sign_in(client, mailbox, email=OWNER_EMAIL)

    subject_id = await user_id_of(client, SUBJECT_EMAIL)
    org_id = str((await client.get("/v1/orgs/current")).json()["id"])
    created = await client.post(
        "/v1/kt",
        json={
            "subject_user_id": subject_id,
            "scope": scope or FULL_SCOPE,
            "validity_days": 30,
            "period_days": period_days,
        },
        headers=csrf(client),
    )
    assert created.status_code == 201, created.text
    package = created.json()

    documents, chunks = await seed_corpus(world.db, org_id=org_id, subject_id=subject_id)

    await sign_in(client, mailbox, email=RECIPIENT_EMAIL)
    claimed = await client.post(
        "/v1/kt/claim", json={"kt_code": package["kt_code"]}, headers=csrf(client)
    )
    assert claimed.status_code == 200, claimed.text
    return Seeded(
        code=str(package["kt_code"]),
        package_id=str(package["id"]),
        org_id=org_id,
        documents=documents,
        chunks=chunks,
    )


async def ask(
    world: World, code: str, question: str = "What was the leaver working on?"
) -> Response:
    return await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question}, headers=csrf(world.http)
    )


def titles(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["document_title"]) for item in items}


# ------------------------------------------------------------ requester B, subject A


class TestTheRecipientReadsTheSubjectsKnowledge:
    """Tests 1-4, 12 and 14: B searches inside A's package and receives A's knowledge."""

    async def test_ask_kt_retrieves_the_subjects_documents_never_the_recipients(
        self, world: World
    ) -> None:
        seeded = await build(world)

        response = await ask(world, seeded.code, "What projects is the employee responsible for?")

        assert response.status_code == 200, response.text
        body = response.json()
        # Set equality, not containment: a leak is a document that is PRESENT, and a
        # containment check would pass with every one of them in the result.
        assert titles(body["sources"]) == IN_PACKAGE
        assert "Zephyr" not in response.text, "the recipient's own corpus answered"

    async def test_every_citation_points_inside_the_package(self, world: World) -> None:
        seeded = await build(world)
        world.scripted.replies = ["Atlas moves the ledger to PostgreSQL [1][2]."]

        answered = (await ask(world, seeded.code)).json()

        assert answered["insufficient_evidence"] is False
        cited = {str(c["document_id"]) for c in answered["citations"]}
        in_package = {seeded.documents["atlas"], seeded.documents["shared"]}
        assert cited and cited <= in_package

        # The kept conversation re-checks against the package, and still finds them.
        replay = (
            await world.http.get(
                f"/v1/kt/{seeded.code}/conversations/{answered['conversation_id']}"
            )
        ).json()
        replayed = [c for m in replay["messages"] for c in m["citations"]]
        assert replayed and all(c["available"] for c in replayed)

        # And each citation's source span opens through the KT door.
        for citation in answered["citations"]:
            span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{citation['chunk_id']}")
            assert span.status_code == 200, span.text

    async def test_the_documents_tab_lists_the_subjects_documents(self, world: World) -> None:
        seeded = await build(world)

        page = await world.http.get(f"/v1/kt/{seeded.code}/documents")

        assert page.status_code == 200, page.text
        assert {item["title"] for item in page.json()["items"]} == IN_PACKAGE

    async def test_the_knowledge_tabs_are_the_subjects_claims(self, world: World) -> None:
        seeded = await build(world)

        insights = await world.http.get(f"/v1/kt/{seeded.code}/insights")
        counts = await world.http.get(f"/v1/kt/{seeded.code}/insights-summary")

        assert insights.status_code == 200, insights.text
        assert {i["name"] for i in insights.json()["items"]} == {"Atlas"}
        assert counts.json()["by_type"] == {"project": 1}

    async def test_the_handover_summary_is_grounded_only_in_the_subjects_claims(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.scripted.replies = ["The leaver owned the Atlas migration [1]."]

        summary = await world.http.get(f"/v1/kt/{seeded.code}/handover-summary")

        assert summary.status_code == 200, summary.text
        assert summary.json()["insufficient_evidence"] is False
        prompt = world.scripted.prompts[0]
        assert "Atlas" in prompt
        for leak in ("Zephyr", "Freeze deploys", "Finance approves", "Colleague decision", "2019"):
            assert leak not in prompt, f"{leak!r} reached the model"

    async def test_the_workspace_counts_what_the_tabs_list(self, world: World) -> None:
        seeded = await build(world)

        workspace = await world.http.get(f"/v1/kt/{seeded.code}/workspace")

        assert workspace.status_code == 200, workspace.text
        coverage = workspace.json()["coverage"]
        assert coverage["documents_visible"] == len(IN_PACKAGE)
        assert {c["claim_type"]: c["claims_visible"] for c in coverage["categories"]}[
            "project"
        ] == 1


# ------------------------------------------------------------- nothing outside the box


class TestNothingOutsideThePackage:
    """Test 5 and the scope: what the subject can reach is not what the package carries."""

    async def test_the_subjects_group_and_org_reach_is_not_the_subjects_knowledge(
        self, world: World
    ) -> None:
        seeded = await build(world)

        sources = titles((await ask(world, seeded.code)).json()["sources"])

        assert "Engineering all-hands" not in sources, "group arm leaked into the package"
        assert "Company handbook" not in sources, "org arm leaked into the package"

    async def test_the_subjects_documents_outside_the_window_are_absent(self, world: World) -> None:
        seeded = await build(world, period_days=90)

        sources = titles((await ask(world, seeded.code)).json()["sources"])
        listed = {
            i["title"]
            for i in (await world.http.get(f"/v1/kt/{seeded.code}/documents")).json()["items"]
        }
        archive_span = await world.http.get(
            f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['archive']}"
        )

        assert "Atlas archive 2019" not in sources
        assert "Atlas archive 2019" not in listed
        assert archive_span.status_code == 404

    async def test_a_chunk_outside_the_package_is_the_same_404_as_one_that_never_existed(
        self, world: World
    ) -> None:
        """Test 17: the evidence door is no oracle for B's corpus, C's or a typo."""
        seeded = await build(world)

        refusals = [
            await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks[key]}")
            for key in ("zephyr", "colleague", "group", "org")
        ]
        never = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{uuid.uuid4()}")

        assert all(r.status_code == 404 for r in [*refusals, never])
        assert len({r.json()["error"]["message"] for r in [*refusals, never]}) == 1

    async def test_a_package_without_documents_refuses_raw_passages_everywhere(
        self, world: World
    ) -> None:
        seeded = await build(world, scope=["projects"])

        assert (await ask(world, seeded.code)).status_code == 403
        assert (await world.http.get(f"/v1/kt/{seeded.code}/documents")).status_code == 403
        span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['atlas']}")
        assert span.status_code == 403
        # The categories it DOES carry still read — the scope narrows, it does not close.
        insights = await world.http.get(f"/v1/kt/{seeded.code}/insights")
        assert {i["name"] for i in insights.json()["items"]} == {"Atlas"}


# --------------------------------------------------- the recipient's own path is intact


class TestTheRecipientsOwnAuthorizationIsUntouched:
    """Test 13: only the KT console changed. B's ordinary reads are still B's."""

    async def test_the_generic_evidence_door_still_answers_to_the_recipients_own_acl(
        self, world: World
    ) -> None:
        seeded = await build(world)

        own = await world.http.get(f"/v1/evidence/{seeded.chunks['zephyr']}")
        subjects = await world.http.get(f"/v1/evidence/{seeded.chunks['atlas']}")

        assert own.status_code == 200, "B lost access to B's own document"
        assert subjects.status_code == 404, "the KT capability leaked into B's normal account"

    async def test_normal_search_returns_the_recipients_corpus_not_the_subjects(
        self, world: World
    ) -> None:
        seeded = await build(world)
        assert seeded.code

        response = await world.http.post(
            "/v1/search", json={"query": "launch"}, headers=csrf(world.http)
        )

        assert response.status_code == 200, response.text
        found = {str(item["document_title"]) for item in response.json()["items"]}
        assert "Zephyr launch notes" in found
        assert "Shared roadmap" in found
        assert "Atlas migration plan" not in found


# ----------------------------------------------------------------- the capability boundary


class TestTheBoundaryHolds:
    """Tests 6-11, 15-17: the package is the only door, and it closes."""

    READ_ROUTES = ("documents", "insights", "insights-summary", "workspace", "handover-summary")

    async def test_another_employee_cannot_use_the_package(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world.http, world.mailbox, email=COLLEAGUE_EMAIL)

        refusals = [
            await world.http.get(f"/v1/kt/{seeded.code}/{route}") for route in self.READ_ROUTES
        ]
        refusals.append(await ask(world, seeded.code))
        refusals.append(
            await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['atlas']}")
        )

        assert [r.status_code for r in refusals] == [404] * len(refusals)
        assert world.scripted.prompts == [], "C reached the model through B's package"

    async def test_another_tenant_cannot_use_the_package(self, world: World) -> None:
        seeded = await build(world)
        await register(world.http, world.mailbox, OTHER_TENANT)

        foreign = await world.http.get(f"/v1/kt/{seeded.code}/documents")

        assert foreign.status_code == 404

    async def test_an_expired_package_refuses(self, world: World) -> None:
        seeded = await build(world)
        await world.db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": seeded.org_id}
        )
        await world.db.execute(
            text("UPDATE kt_packages SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": seeded.package_id},
        )
        await world.db.commit()

        response = await ask(world, seeded.code)

        assert response.status_code == 403
        assert "expired" in response.json()["error"]["message"].lower()

    async def test_a_revoked_package_refuses(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world.http, world.mailbox, email=OWNER_EMAIL)
        revoked = await world.http.post(
            f"/v1/kt/{seeded.package_id}/revoke", headers=csrf(world.http)
        )
        assert revoked.status_code == 200, revoked.text
        await sign_in(world.http, world.mailbox, email=RECIPIENT_EMAIL)

        response = await ask(world, seeded.code)

        assert response.status_code == 403
        assert "revoked" in response.json()["error"]["message"].lower()

    async def test_a_completed_package_refuses(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world.http, world.mailbox, email=OWNER_EMAIL)
        completed = await world.http.post(
            f"/v1/kt/{seeded.package_id}/complete", headers=csrf(world.http)
        )
        assert completed.status_code == 200, completed.text
        await sign_in(world.http, world.mailbox, email=RECIPIENT_EMAIL)

        response = await world.http.get(f"/v1/kt/{seeded.code}/documents")

        assert response.status_code == 403
        assert "complete" in response.json()["error"]["message"].lower()

    async def test_an_invalid_code_is_refused_exactly_like_a_package_that_is_not_yours(
        self, world: World
    ) -> None:
        seeded = await build(world)
        await sign_in(world.http, world.mailbox, email=COLLEAGUE_EMAIL)

        not_yours = await world.http.get(f"/v1/kt/{seeded.code}/documents")
        invalid = await world.http.get("/v1/kt/KT-JUTSU-00000000/documents")

        assert not_yours.status_code == invalid.status_code == 404
        assert not_yours.json()["error"]["message"] == invalid.json()["error"]["message"]

    async def test_no_subject_email_principal_or_storage_internal_reaches_the_recipient(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.scripted.replies = ["Atlas [1].", "The leaver owned Atlas [1]."]

        bodies = [
            (await ask(world, seeded.code)).text,
            (await world.http.get(f"/v1/kt/{seeded.code}/documents")).text,
            (await world.http.get(f"/v1/kt/{seeded.code}/insights")).text,
            (await world.http.get(f"/v1/kt/{seeded.code}/workspace")).text,
            (await world.http.get(f"/v1/kt/{seeded.code}/handover-summary")).text,
            (await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.chunks['atlas']}")).text,
        ]

        for body in bodies:
            assert SUBJECT_EMAIL not in body
            assert SUBJECT not in body, "a provider principal is personal data (§4.9)"
            for internal in ("gs://", "object_key", "storage_path", "signed_url"):
                assert internal not in body


# ----------------------------------------------------------------- the handover report


class TestTheHandoverReport:
    """Compose summary is a real PDF of the subject's knowledge, over Ask KT's boundary.

    Proven by decoding the PDF the API returned and reading it back — the same bytes a
    recipient downloads — not by checking that a renderer was called.
    """

    async def compose(self, world: World, code: str) -> Response:
        return await world.http.post(f"/v1/kt/{code}/handover-report", headers=csrf(world.http))

    async def test_it_is_a_real_pdf_of_the_subjects_knowledge_and_nothing_else(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.scripted.replies = ["The leaver owned the Atlas migration [1]."]

        response = await self.compose(world, seeded.code)

        assert response.status_code == 200, response.text
        body = response.json()
        pdf = base64.b64decode(body["pdf_base64"])
        assert pdf.startswith(b"%PDF-")
        text_in_pdf = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
        assert "Knowledge Transfer — Handover Summary" in text_in_pdf
        assert "The leaver owned the Atlas migration [1]." in text_in_pdf
        assert "[1] Atlas migration plan" in text_in_pdf
        assert "Shared roadmap" in text_in_pdf, "the subject's other document is listed"
        for leak in (
            "Zephyr",
            "Colleague private notes",
            "Colleague decision",
            "Engineering all-hands",
            "Freeze deploys",
            "Company handbook",
            "Finance approves",
            "Atlas archive",
            "Atlas 2019",
        ):
            assert leak not in text_in_pdf, f"{leak!r} reached the PDF"
        assert SUBJECT_EMAIL not in text_in_pdf
        assert SUBJECT not in text_in_pdf

        assert body["summary"] == "The leaver owned the Atlas migration [1]."
        assert [r["document_title"] for r in body["references"]] == ["Atlas migration plan"]
        assert body["filename"].startswith("jutsu-handover-summary-")
        assert body["filename"].endswith(".pdf")
        assert len(world.scripted.prompts) == 1, "one press, one model call"
        assert "Zephyr" not in world.scripted.prompts[0]

    async def test_it_is_refused_exactly_like_every_other_kt_read(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world.http, world.mailbox, email=COLLEAGUE_EMAIL)

        stranger = await self.compose(world, seeded.code)
        invalid = await self.compose(world, "KT-JUTSU-00000000")

        assert stranger.status_code == invalid.status_code == 404
        assert stranger.json()["error"]["message"] == invalid.json()["error"]["message"]

        await sign_in(world.http, world.mailbox, email=OWNER_EMAIL)
        revoked = await world.http.post(
            f"/v1/kt/{seeded.package_id}/revoke", headers=csrf(world.http)
        )
        assert revoked.status_code == 200, revoked.text
        await sign_in(world.http, world.mailbox, email=RECIPIENT_EMAIL)

        closed = await self.compose(world, seeded.code)

        assert closed.status_code == 403
        assert "revoked" in closed.json()["error"]["message"].lower()
        assert world.scripted.prompts == [], "no refused caller reached the model"

    async def test_without_an_answer_model_it_refuses_before_spending(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seeded = await build(world)
        unconfigure_answers(monkeypatch)

        response = await self.compose(world, seeded.code)

        assert response.status_code == 503
        assert "not configured" in response.json()["error"]["message"]
        assert world.scripted.prompts == []

    async def test_it_requires_the_csrf_token_like_every_state_changing_call(
        self, world: World
    ) -> None:
        """It spends a budget and writes an audit row, so a cross-site POST must not."""
        seeded = await build(world)

        forged = await world.http.post(f"/v1/kt/{seeded.code}/handover-report")

        assert forged.status_code == 401
        assert world.scripted.prompts == [], "a forged request never reached the model"

    async def test_one_press_writes_one_audit_row_of_counts_and_no_narrative(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.scripted.replies = ["The leaver owned the Atlas migration [1]."]
        assert (await self.compose(world, seeded.code)).status_code == 200

        await world.db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": seeded.org_id}
        )
        rows = (
            await world.db.execute(
                text(
                    "SELECT resource_id, meta_json FROM audit_log "
                    "WHERE action = 'kt.handover_report'"
                )
            )
        ).all()

        assert len(rows) == 1
        assert rows[0].resource_id == seeded.package_id
        assert set(rows[0].meta_json) == {
            "insufficient_evidence",
            "citations",
            "attempts",
            "claims_considered",
        }
        assert "Atlas" not in str(rows[0].meta_json), "the narrative never reaches the trail"


# ---------------------------------------------------------------------------- the logs


def emitted(records: list[logging.LogRecord]) -> list[str]:
    """Each captured record as the service writes it.

    Rendered by the JSON handler `create_app` installed. Its filters have already run on
    these very objects — logging hands one record to every handler — so this is the line
    Cloud Logging receives, redactions included, not a re-derivation of it.
    """
    installed = [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
    assert len(installed) == 1, "the application's JSON handler is not installed"
    return [installed[0].format(record) for record in records]


class TestTheLogs:
    """Five safe events, and none of what a KT log line must never hold.

    Captured from the owner creating the package, through B's claim, question and report,
    to C's refused attempt — at DEBUG, because a value leaked at a level the deployment
    does not emit today is one environment variable away from being emitted.
    """

    QUESTION = "SENTINEL-QUESTION-5F: which migration did the leaver own?"
    ANSWER = "SENTINEL-ANSWER-5F: the leaver owned the Atlas migration [1]."

    EVENTS = frozenset(
        {
            "kt_retrieval_context_created",
            "kt_search_started",
            "kt_search_completed",
            "kt_summary_started",
            "kt_summary_completed",
        }
    )

    #: All one of those events may carry: the formatter's own keys, the request context
    #: (opaque ids), the package id, and counts, flags and timings.
    SAFE_KEYS = frozenset(
        {
            "severity",
            "level",
            "logger",
            "message",
            "msg",
            "request_id",
            "org_id",
            "user_id",
            "event",
            "package_id",
            "subject_principals",
            "categories",
            "windowed",
            "k",
            "results",
            "elapsed_ms",
            "claims",
            "citations",
            "sources",
            "insufficient_evidence",
        }
    )

    async def test_the_kt_events_carry_counts_never_a_code_an_address_or_content(
        self,
        world: World,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        # The test's own client logs every URL it requests, code included: the harness
        # talking, not the service, whose outbound httpx calls go to model vendors and never
        # carry a KT code. Disabled, NOT `caplog.set_level(WARNING, logger="httpx")` — that
        # also lowers caplog's own handler to WARNING, which silently stopped capturing every
        # INFO event here and left the leak checks below reading two lines.
        monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)
        seeded = await build(world)
        world.scripted.replies = [self.ANSWER, self.ANSWER]

        asked = await ask(world, seeded.code, self.QUESTION)
        composed = await world.http.post(
            f"/v1/kt/{seeded.code}/handover-report", headers=csrf(world.http)
        )
        await sign_in(world.http, world.mailbox, email=COLLEAGUE_EMAIL)
        refused = await ask(world, seeded.code, self.QUESTION)
        assert (asked.status_code, composed.status_code, refused.status_code) == (200, 200, 404)

        lines = emitted(caplog.records)
        parsed = [json.loads(line) for line in lines]

        # That the capture saw the events at all comes first: every check after it is
        # vacuous over a capture that missed them.
        seen = [line for line in parsed if line.get("event") in self.EVENTS]
        assert {line["event"] for line in seen} == self.EVENTS
        for line in seen:
            extra = sorted(set(line) - self.SAFE_KEYS)
            assert not extra, f"{line['event']} carries {extra}"
            assert line["package_id"] == seeded.package_id
        (search,) = [line for line in seen if line["event"] == "kt_search_completed"]
        assert search["results"] == len(IN_PACKAGE)

        never = {
            "the KT code": seeded.code,
            "the KT code in lower case": seeded.code.lower(),
            "the owner's address": OWNER_EMAIL,
            "the subject's address": SUBJECT_EMAIL,
            "the recipient's address": RECIPIENT_EMAIL,
            "the colleague's address": COLLEAGUE_EMAIL,
            "the question": "SENTINEL-QUESTION-5F",
            "the model's answer": "SENTINEL-ANSWER-5F",
            "a passage": "Atlas moves the ledger",
            "another passage": "The roadmap ships Atlas",
            "the recipient's own passage": "Zephyr is the newhire",
            "a document title": "Atlas migration plan",
        }
        for what, value in never.items():
            leaked = [line for line in lines if value in line]
            assert not leaked, f"{what} reached {len(leaked)} log line(s): {leaked[0][:240]}"

        # The refusal WAS logged, with the route it hit — the code is what is missing.
        refusals = [
            line
            for line in parsed
            if line.get("event") == "request_failed" and line.get("status") == 404
        ]
        assert refusals, "C's refusal never reached a log line"
        assert all(str(line["path"]).endswith("/ask") for line in refusals)


# --------------------------------------------------------------- the scope's provenance


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _enclosing_functions(tree: ast.AST, name: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _calls(node, name):
            # Innermost definition wins: a nested def would otherwise be counted twice.
            inner = [
                child
                for child in ast.walk(node)
                if child is not node
                and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                and _calls(child, name)
            ]
            if not inner:
                found.append(node.name)
    return found


def _sources() -> dict[str, ast.AST]:
    return {
        str(path.relative_to(API_SRC)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(API_SRC.rglob("*.py"))
    }


class TestTheScopeIsStructural:
    """What a behavioural test cannot see: where a scope, and a subject, can come from."""

    def test_a_scope_is_constructed_in_exactly_one_place(self) -> None:
        sites = {
            file: _enclosing_functions(tree, "KtScope")
            for file, tree in _sources().items()
            if _calls(tree, "KtScope")
        }
        assert sites == {"kt.py": ["_scope_for"]}

    def test_the_subject_search_and_evidence_each_have_one_caller(self) -> None:
        search = {
            file: _enclosing_functions(tree, "search_subject_chunks")
            for file, tree in _sources().items()
            if _calls(tree, "search_subject_chunks")
        }
        evidence = {
            file: _enclosing_functions(tree, "fetch_subject_evidence")
            for file, tree in _sources().items()
            if _calls(tree, "fetch_subject_evidence")
        }
        assert search == {"kt_workspace.py": ["ask_copilot"]}
        assert evidence == {"kt.py": ["kt_evidence"]}

    def test_no_kt_route_resolves_the_requesters_principals(self) -> None:
        for router in ("routers/kt.py", "routers/kt_console.py"):
            source = (API_SRC / router).read_text(encoding="utf-8")
            assert "scoped_acl_principals" not in source, router

    def test_no_kt_reader_accepts_a_principal_set(self) -> None:
        from jutsu_api import kt, kt_workspace

        readers = (
            kt.kt_documents,
            kt.kt_document,
            kt.kt_evidence,
            kt.kt_insights,
            kt.kt_insight_summary,
            kt.kt_handover_summary,
            kt_workspace.ask_copilot,
            kt_workspace.read_conversation,
            kt_workspace.add_bookmark,
            kt_workspace.list_bookmarks,
            kt_workspace.read_workspace,
        )
        for reader in readers:
            parameters = set(inspect.signature(reader).parameters)
            assert not parameters & {"principals", "groups"}, reader.__name__
