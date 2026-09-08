"""The KT console's foundation, over the wire against real Postgres and RLS.

Four things the console rests on, each pinned here because each looks like it works
without the test: the door is budgeted, so a code cannot be probed; a package bound to
somebody else confirms nothing to the wrong holder, whatever its state; every open and
every read leaves a trace an administrator can join to a request; and the paid summary
has a ceiling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}

OWNER_EMAIL = "ada@example.com"


class FakeEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return [0.0] * 768, 7


class ScriptedModel:
    """Never reached in this module: every summary here has no visible claims to ground
    on, so the synthesiser refuses before a call. Present so the seam is complete."""

    async def complete(self, *, system: str, prompt: str) -> str:
        return "INSUFFICIENT_EVIDENCE"


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """The same shape as test_kt.py's client: the budget spends and the denied-open
    audit commit on `jutsu_db.engine`'s own engine, which must be on the application
    role and disposed around every test."""
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_query_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: ScriptedModel()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http

    await dispose_engine()


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> None:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    response = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert response.status_code == 200, response.text


async def invite_and_accept(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    *,
    email: str,
    full_name: str = "Grace Hopper",
    role: str = "member",
) -> None:
    invited = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": role},
        headers=csrf(client),
    )
    assert invited.status_code == 202, invited.text
    token = mailbox.last.secrets["token"]
    accepted = await client.post(
        "/v1/invitations/accept", json={"token": token, "full_name": full_name}
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


async def create_kt(
    client: AsyncClient,
    *,
    subject_user_id: str,
    scope: list[str] | None = None,
    recipient_email: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "subject_user_id": subject_user_id,
        "scope": scope or ["documents", "profile"],
        "validity_days": 30,
    }
    if recipient_email:
        payload["recipient_email"] = recipient_email
    response = await client.post("/v1/kt", json=payload, headers=csrf(client))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def claim(client: AsyncClient, code: str) -> Response:
    """The claim call, typed as what it is.

    It used to be annotated `object`, which made every assertion on the result a mypy
    error and put a `type: ignore[attr-defined]` on twenty-five lines of this file —
    noise that hides a real one. The helper knows the type; saying so removes them all.
    """
    return await client.post("/v1/kt/claim", json={"kt_code": code}, headers=csrf(client))


async def owner_with_package(
    client: AsyncClient, mailbox: RecordingEmailSender, **kwargs: object
) -> dict[str, object]:
    """Owner registered, a leaver invited, a package created for them. Owner stays
    signed in."""
    await register_owner(client, mailbox)
    await invite_and_accept(client, mailbox, email="leaver@example.com")
    await sign_in(client, mailbox, email=OWNER_EMAIL)
    subject = await user_id_of(client, "leaver@example.com")
    return await create_kt(client, subject_user_id=subject, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------


class TestTheDoorIsBudgeted:
    async def test_guesses_are_refused_after_the_budget_with_a_retry_after(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two wrong guesses, then a 429 — before the lookup, so the third guess costs
        nothing and learns nothing."""
        monkeypatch.setenv("KT_CLAIM_RATE_LIMIT", "2")
        await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        first = await claim(client, "KT-JUTSU-00000001")
        second = await claim(client, "KT-JUTSU-00000002")
        third = await claim(client, "KT-JUTSU-00000003")

        assert first.status_code == 404
        assert second.status_code == 404
        assert third.status_code == 429, third.text
        assert third.headers["retry-after"] == "60"
        body = third.json()
        assert body["error"]["code"] == "rate_limited"
        assert "package" in body["error"]["message"]
        # Never the code that was tried: the message is configuration, not data.
        assert "00000003" not in third.text

    async def test_a_refused_guess_spends_the_budget_the_right_code_needed(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attempts are what a probe repeats, so attempts are what is counted."""
        monkeypatch.setenv("KT_CLAIM_RATE_LIMIT", "1")
        package = await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        assert (await claim(client, "KT-JUTSU-00000001")).status_code == 404
        blocked = await claim(client, str(package["kt_code"]))
        assert blocked.status_code == 429

    async def test_the_claim_budget_is_not_the_search_budget(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Buckets are independent: exhausting the door leaves search intact."""
        monkeypatch.setenv("KT_CLAIM_RATE_LIMIT", "1")
        await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        assert (await claim(client, "KT-JUTSU-00000001")).status_code == 404
        assert (await claim(client, "KT-JUTSU-00000002")).status_code == 429

        search = await client.post("/v1/search", json={"query": "anything"}, headers=csrf(client))
        assert search.status_code == 200, search.text


class TestBindingBeforeState:
    async def test_a_revoked_package_bound_to_someone_else_is_still_a_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The wrong holder must not learn that a package exists AND was closed.

        Before this, the revoked/expired checks ran first, so any member of the
        organisation holding a bound code was told "revoked" — an existence signal.
        The right person still gets the exact sentence.
        """
        package = await owner_with_package(client, mailbox, recipient_email="newhire@example.com")
        code = str(package["kt_code"])

        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, code)).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        revoked = await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))
        assert revoked.status_code == 200, revoked.text

        # A third person, in the same organisation, holding the code.
        await invite_and_accept(client, mailbox, email="other@example.com", full_name="Other")
        wrong_holder = await claim(client, code)
        assert wrong_holder.status_code == 404, wrong_holder.text
        assert "revoked" not in wrong_holder.text.lower()

        # The recipient it was bound to sees exactly why it is closed.
        await sign_in(client, mailbox, email="newhire@example.com")
        recipient = await claim(client, code)
        assert recipient.status_code == 403
        assert (
            recipient.json()["error"]["message"]
            == "This Knowledge Transfer package has been revoked."
        )

    async def test_an_addressed_but_unclaimed_expired_package_is_a_404_to_others(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Addressed by email and never claimed: the address decides who may learn
        the state, exactly as it decides who may claim."""
        package = await owner_with_package(client, mailbox, recipient_email="newhire@example.com")
        # The GUC is transaction-local and the request that set it has committed, so a
        # raw write from the test re-enters the tenant the same way the request did —
        # under RLS an unscoped UPDATE affects zero rows and says nothing.
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
        )
        await db_session.execute(
            text("UPDATE kt_packages SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": package["id"]},
        )
        await db_session.commit()

        await invite_and_accept(client, mailbox, email="other@example.com", full_name="Other")
        assert (await claim(client, str(package["kt_code"]))).status_code == 404


class TestTheTrailNamesTheRequest:
    async def test_a_reopen_is_audited_and_joined_to_its_request(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """First open writes kt.claimed; every later one writes kt.opened, carrying the
        request id the response also carried — the join §25 asks for."""
        package = await owner_with_package(client, mailbox)
        code = str(package["kt_code"])
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        first = await claim(client, code)
        again = await claim(client, code)
        assert first.status_code == 200 and again.status_code == 200
        request_id = again.headers["x-request-id"]

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        claimed = (await client.get("/v1/audit", params={"action": "kt.claimed"})).json()
        opened = (await client.get("/v1/audit", params={"action": "kt.opened"})).json()
        assert len(claimed["items"]) == 1
        assert len(opened["items"]) == 1
        assert opened["items"][0]["correlation_id"] == request_id

    async def test_lifecycle_rows_carry_the_request_id(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="leaver@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")

        created = await client.post(
            "/v1/kt",
            json={"subject_user_id": subject, "scope": ["documents"], "validity_days": 7},
            headers=csrf(client),
        )
        assert created.status_code == 201
        trail = (await client.get("/v1/audit", params={"action": "kt.created"})).json()
        assert trail["items"][0]["correlation_id"] == created.headers["x-request-id"]

    async def test_reads_move_last_activity(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The admin figure is "last activity", so a read must count as activity."""
        package = await owner_with_package(client, mailbox)
        code = str(package["kt_code"])
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, code)).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        before = (await client.get(f"/v1/kt/{package['id']}")).json()["last_activity_at"]
        assert before is not None

        await sign_in(client, mailbox, email="newhire@example.com")
        assert (await client.get(f"/v1/kt/{code}/documents")).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        after = (await client.get(f"/v1/kt/{package['id']}")).json()["last_activity_at"]
        assert after >= before


class TestTheSummaryHasACeiling:
    async def test_summaries_are_budgeted(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One paid call per press, so one budget per press — spent after the free
        configuration gate and before anything else."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("KT_SUMMARY_RATE_LIMIT", "1")
        package = await owner_with_package(client, mailbox, scope=["decisions", "projects"])
        code = str(package["kt_code"])
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, code)).status_code == 200

        first = await client.get(f"/v1/kt/{code}/handover-summary")
        second = await client.get(f"/v1/kt/{code}/handover-summary")

        assert first.status_code == 200, first.text
        assert first.json()["insufficient_evidence"] is True
        assert second.status_code == 429, second.text
        assert second.headers["retry-after"] == "60"

    async def test_an_unconfigured_summary_costs_no_budget(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 503 comes first and is free: a deployment fact should not eat quota."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("KT_SUMMARY_RATE_LIMIT", "1")
        package = await owner_with_package(client, mailbox)
        code = str(package["kt_code"])
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, code)).status_code == 200

        for _ in range(3):
            response = await client.get(f"/v1/kt/{code}/handover-summary")
            assert response.status_code == 503, response.text


class TestAdminUpdates:
    """PATCH /v1/kt/{id}: the two edits an administrator legitimately needs after creation,
    each bounded like creation and each its own audit row."""

    async def owner_package(
        self, client: AsyncClient, mailbox: RecordingEmailSender, **kwargs: object
    ) -> dict[str, object]:
        return await owner_with_package(client, mailbox, **kwargs)

    async def test_extending_moves_the_expiry_and_is_audited_with_before_and_after(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        package = await self.owner_package(client, mailbox)
        before = str(package["expires_at"])

        response = await client.patch(
            f"/v1/kt/{package['id']}", json={"extend_days": 10}, headers=csrf(client)
        )
        assert response.status_code == 200, response.text
        after = str(response.json()["expires_at"])
        assert after > before

        await scope_to_caller(client, db_session)
        rows = (
            await db_session.execute(
                text("SELECT meta_json FROM audit_log WHERE action = 'kt.extended'")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].meta_json["expires_at"]["to"] > rows[0].meta_json["expires_at"]["from"]

    async def test_a_lapsed_package_can_be_reopened_by_extension(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        package = await self.owner_package(client, mailbox)
        await scope_to_caller(client, db_session)
        await db_session.execute(
            text("UPDATE kt_packages SET expires_at = now() - interval '2 days' WHERE id = :id"),
            {"id": package["id"]},
        )
        await db_session.commit()

        assert (await client.get(f"/v1/kt/{package['id']}")).json()["status"] == "expired"
        response = await client.patch(
            f"/v1/kt/{package['id']}", json={"extend_days": 7}, headers=csrf(client)
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"

        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, str(package["kt_code"]))).status_code == 200

    async def test_extension_is_bounded_a_year_from_today(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        package = await self.owner_package(client, mailbox)  # 30 days of validity
        too_far = await client.patch(
            f"/v1/kt/{package['id']}", json={"extend_days": 360}, headers=csrf(client)
        )
        assert too_far.status_code == 422
        nothing = await client.patch(f"/v1/kt/{package['id']}", json={}, headers=csrf(client))
        assert nothing.status_code == 422

    async def test_readdressing_an_unclaimed_package_moves_who_may_claim(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        package = await self.owner_package(client, mailbox, recipient_email="first@example.com")
        code = str(package["kt_code"])

        moved = await client.patch(
            f"/v1/kt/{package['id']}",
            json={"recipient_email": "second@example.com"},
            headers=csrf(client),
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["recipient_email"] == "second@example.com"

        await invite_and_accept(client, mailbox, email="first@example.com", full_name="First")
        assert (await claim(client, code)).status_code == 404

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="second@example.com", full_name="Second")
        assert (await claim(client, code)).status_code == 200

    async def test_a_claimed_package_keeps_its_recipient(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        package = await self.owner_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        assert (await claim(client, str(package["kt_code"]))).status_code == 200

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        refused = await client.patch(
            f"/v1/kt/{package['id']}",
            json={"recipient_email": "other@example.com"},
            headers=csrf(client),
        )
        assert refused.status_code == 409
        assert "claimed" in refused.json()["error"]["message"]

    async def test_a_revoked_package_refuses_every_change(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        package = await self.owner_package(client, mailbox)
        assert (
            await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))
        ).status_code == 200
        response = await client.patch(
            f"/v1/kt/{package['id']}", json={"extend_days": 5}, headers=csrf(client)
        )
        assert response.status_code == 409
        assert "revoked" in response.json()["error"]["message"]

    async def test_a_member_may_not_update(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        package = await self.owner_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com", full_name="M")
        response = await client.patch(
            f"/v1/kt/{package['id']}", json={"extend_days": 5}, headers=csrf(client)
        )
        assert response.status_code == 403


async def scope_to_caller(client: AsyncClient, session: AsyncSession) -> None:
    org_id = (await client.get("/v1/me")).json()["org_id"]
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
    )


class TestTheSiblingRoutesAreWalledToo:
    """The claim door was walled and the twelve routes beside it were not.

    Every KT route takes a caller-supplied code and reaches the same lookup through
    `_open_for`, so guessing against `GET /v1/kt/{code}/documents` cost nothing while
    `POST /v1/kt/claim` was refused after ten attempts a minute. These tests pin the
    second wall, and — as much — pin the two things that make it a wall rather than a
    hint: it is a *different* bucket from the claim door, and it is charged before the
    lookup so a hit and a miss cost the same.
    """

    async def test_guessing_through_a_sibling_route_runs_out_of_budget(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KT_OPEN_RATE_LIMIT", "2")
        await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        first = await client.get("/v1/kt/KT-JUTSU-00000001/documents")
        second = await client.get("/v1/kt/KT-JUTSU-00000002/documents")
        third = await client.get("/v1/kt/KT-JUTSU-00000003/documents")

        assert first.status_code == 404
        assert second.status_code == 404
        assert third.status_code == 429, third.text
        assert third.json()["error"]["code"] == "rate_limited"
        # Configuration, never the code that was tried.
        assert "00000003" not in third.text

    async def test_a_real_code_costs_the_same_as_a_wrong_one(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Charging only on a miss would build the oracle the 404 exists to refuse.

        If a hit were free, then once the budget was spent a 429 would mean "no such
        package" and any other status would mean "there is one" — which makes probing
        cheaper rather than dearer. So the allowance is spent before the lookup, and a
        recipient opening their own package spends it too.
        """
        monkeypatch.setenv("KT_OPEN_RATE_LIMIT", "1")
        package = await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")
        code = str(package["kt_code"])
        # Claimed through the POST door first, because a GET may no longer bind an
        # unaddressed package — reading must not decide whose package it is. The claim
        # spends `KT_CLAIM`, a different bucket, so it leaves the `KT_OPEN` allowance of 1
        # intact and this still measures exactly what it measured before.
        assert (
            await client.post("/v1/kt/claim", json={"kt_code": code}, headers=csrf(client))
        ).status_code == 200

        assert (await client.get(f"/v1/kt/{code}/documents")).status_code == 200
        assert (await client.get(f"/v1/kt/{code}/documents")).status_code == 429

    async def test_the_claim_door_keeps_its_own_allowance(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two buckets, because one person's reading is not one prober's guessing.

        A KT session mounts several queries per panel and would spend the claim
        allowance in seconds if the two shared a bucket — which is what makes this a
        separate `Bucket` rather than a wider limit on the existing one.
        """
        monkeypatch.setenv("KT_OPEN_RATE_LIMIT", "1")
        monkeypatch.setenv("KT_CLAIM_RATE_LIMIT", "5")
        await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        assert (await client.get("/v1/kt/KT-JUTSU-00000001/documents")).status_code == 404
        assert (await client.get("/v1/kt/KT-JUTSU-00000002/documents")).status_code == 429

        # The door is untouched: exhausting one bucket must not close the other.
        assert (await claim(client, "KT-JUTSU-00000003")).status_code == 404

    async def test_one_claim_attempt_costs_exactly_one_allowance(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression this class was written for.

        `claim` spends `KT_CLAIM` before its lookup and then calls `_open_for`. When
        that function grew its own charge, one attempt cost two — so a limit of five
        refused the third guess, and the stated allowance was quietly halved.
        """
        monkeypatch.setenv("KT_CLAIM_RATE_LIMIT", "3")
        await owner_with_package(client, mailbox)
        await invite_and_accept(client, mailbox, email="newhire@example.com", full_name="New")

        for attempt in range(3):
            response = await claim(client, f"KT-JUTSU-0000000{attempt + 1}")
            assert response.status_code == 404, f"attempt {attempt + 1}: {response.text}"

        assert (await claim(client, "KT-JUTSU-00000009")).status_code == 429
