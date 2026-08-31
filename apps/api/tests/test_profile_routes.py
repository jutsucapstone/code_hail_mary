"""`/v1/me/profile` — the first reader and writer of a table that shipped in 0002.

`employee_profiles` has had row-level security enabled and FORCED since migration 0002,
and until now no application code touched it. These tests exercise the real policy
against a real Postgres: nothing here mocks the database, because the claim under test is
that the *policy* holds, and a mock cannot be wrong in the way a policy can.

The adversarial cases are the point. A profile endpoint is a small surface with two
sharp edges — a client that names somebody else's user id, and a client that names
another tenant — and both are checked here rather than reasoned about.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.profiles import ProfileUpdate, read_profile, upsert_profile
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_core.errors import NotFound
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


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


@pytest.fixture
async def client(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as http:
        yield http


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> dict[str, Any]:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text
    me = (await client.get("/v1/me")).json()
    return {"user_id": uuid.UUID(me["user_id"]), "org_id": uuid.UUID(me["org_id"])}


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def add_user(session: AsyncSession, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": email},
    )
    return user_id


# ------------------------------------------------------------------------------- reading


class TestReadingYourOwnProfile:
    async def test_a_missing_profile_is_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """A normal state, not a fault.

        Migration 0002 says an owner is a `users` row with no profile. The endpoint says
        so with a 404 rather than 200-with-nulls, because "never saved" and "saved empty"
        are different — the second has an `updated_at`.
        """
        await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.get("/v1/me/profile")

        assert response.status_code == 404, response.text

    async def test_a_saved_profile_comes_back(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        await client.patch(
            "/v1/me/profile",
            json={"department": "Engineering", "designation": "Principal"},
            headers=csrf(client),
        )

        body = (await client.get("/v1/me/profile")).json()

        assert body["department"] == "Engineering"
        assert body["designation"] == "Principal"
        assert body["skills"] == []

    async def test_the_response_carries_no_identity_fields(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """No `user_id`, no `org_id`. Nothing here can be mistaken for an input."""
        await register_owner(client, mailbox)
        await db_session.commit()
        await client.patch("/v1/me/profile", json={"department": "Ops"}, headers=csrf(client))

        body = (await client.get("/v1/me/profile")).json()

        assert "user_id" not in body
        assert "org_id" not in body

    async def test_an_anonymous_caller_is_401(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/me/profile")).status_code == 401


# ------------------------------------------------------------------------------- writing


class TestWritingYourOwnProfile:
    async def test_the_first_patch_creates_the_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        who = await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.patch(
            "/v1/me/profile",
            json={"employee_code": "E-1", "skills": ["python", "sql"]},
            headers=csrf(client),
        )

        assert response.status_code == 200, response.text
        assert response.json()["skills"] == ["python", "sql"]

        await scope(db_session, who["org_id"])
        count = (
            await db_session.execute(
                text("SELECT count(*) FROM employee_profiles WHERE user_id = :u"),
                {"u": str(who["user_id"])},
            )
        ).scalar()
        assert count == 1

    async def test_patching_twice_never_makes_a_second_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """`user_id` is the primary key, so the upsert is idempotent by construction."""
        who = await register_owner(client, mailbox)
        await db_session.commit()

        for department in ("Engineering", "Platform", "Data"):
            assert (
                await client.patch(
                    "/v1/me/profile", json={"department": department}, headers=csrf(client)
                )
            ).status_code == 200

        await scope(db_session, who["org_id"])
        rows = (
            await db_session.execute(
                text("SELECT count(*) FROM employee_profiles WHERE user_id = :u"),
                {"u": str(who["user_id"])},
            )
        ).scalar()
        assert rows == 1
        assert (await client.get("/v1/me/profile")).json()["department"] == "Data"

    async def test_an_omitted_field_is_left_alone(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The difference between PATCH and PUT, and the reason `model_fields_set` exists."""
        await register_owner(client, mailbox)
        await db_session.commit()
        await client.patch(
            "/v1/me/profile",
            json={"department": "Engineering", "designation": "Principal"},
            headers=csrf(client),
        )

        await client.patch("/v1/me/profile", json={"department": "Platform"}, headers=csrf(client))

        body = (await client.get("/v1/me/profile")).json()
        assert body["department"] == "Platform"
        assert body["designation"] == "Principal", "an omitted field was nulled"

    async def test_an_explicit_null_clears_the_field(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The other half of the same distinction: sending null must mean "clear it"."""
        await register_owner(client, mailbox)
        await db_session.commit()
        await client.patch(
            "/v1/me/profile", json={"designation": "Principal"}, headers=csrf(client)
        )

        await client.patch("/v1/me/profile", json={"designation": None}, headers=csrf(client))

        assert (await client.get("/v1/me/profile")).json()["designation"] is None

    async def test_an_anonymous_write_is_401(self, client: AsyncClient) -> None:
        assert (await client.patch("/v1/me/profile", json={"department": "X"})).status_code == 401


# ------------------------------------------------------------------- refusing the inputs


class TestTheBodyCannotCarryIdentity:
    @pytest.mark.parametrize("field", ["user_id", "org_id"])
    async def test_ownership_and_tenancy_are_refused_outright(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        field: str,
    ) -> None:
        """`extra="forbid"` — refused with a 422, not silently ignored.

        Ignoring them would be safe today and a trap tomorrow: the moment somebody widens
        this model, a field that was being dropped starts being honoured.
        """
        await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.patch(
            "/v1/me/profile",
            json={"department": "Engineering", field: str(uuid.uuid4())},
            headers=csrf(client),
        )

        assert response.status_code == 422, response.text

    async def test_a_rejected_body_writes_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()

        await client.patch(
            "/v1/me/profile",
            json={"department": "Engineering", "org_id": str(uuid.uuid4())},
            headers=csrf(client),
        )

        assert (await client.get("/v1/me/profile")).status_code == 404, "a refused patch wrote"

    @pytest.mark.parametrize(
        "body",
        [
            {"employee_code": "x" * 65},
            {"department": "d" * 129},
            {"designation": "d" * 129},
            {"phone_e164": "07700900000"},
            {"phone_e164": "+0123456789"},
            {"joining_date": "not-a-date"},
            {"skills": "python"},
            {"unknown_field": "x"},
        ],
    )
    async def test_invalid_values_are_422(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        body: dict[str, Any],
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.patch("/v1/me/profile", json=body, headers=csrf(client))

        assert response.status_code == 422, f"{body} was accepted: {response.text}"

    async def test_a_valid_e164_number_is_accepted(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.patch(
            "/v1/me/profile", json={"phone_e164": "+919876543210"}, headers=csrf(client)
        )

        assert response.status_code == 200, response.text
        assert response.json()["phone_e164"] == "+919876543210"


# ------------------------------------------------------------------------ isolation


class TestNobodyElsesProfile:
    async def test_a_colleagues_profile_is_not_readable_through_this_endpoint(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Same tenant, different person. The `user_id` predicate is what stops this.

        Row-level security alone would not: a colleague's row is inside the same
        organisation and therefore passes the policy.
        """
        who = await register_owner(client, mailbox)
        colleague = await add_user(db_session, who["org_id"], "colleague@example.com")
        await db_session.execute(
            text(
                "INSERT INTO employee_profiles (user_id, org_id, department) "
                "VALUES (:u, :o, 'Secret Department')"
            ),
            {"u": colleague, "o": who["org_id"]},
        )
        await db_session.commit()

        # The caller has no profile of their own, so anything but a 404 means the
        # endpoint reached somebody else's row.
        response = await client.get("/v1/me/profile")

        assert response.status_code == 404, response.text
        assert "Secret Department" not in response.text

    async def test_a_write_cannot_touch_a_colleagues_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        who = await register_owner(client, mailbox)
        colleague = await add_user(db_session, who["org_id"], "colleague@example.com")
        await db_session.execute(
            text(
                "INSERT INTO employee_profiles (user_id, org_id, department) "
                "VALUES (:u, :o, 'Untouched')"
            ),
            {"u": colleague, "o": who["org_id"]},
        )
        await db_session.commit()

        await client.patch("/v1/me/profile", json={"department": "Mine"}, headers=csrf(client))

        await scope(db_session, who["org_id"])
        theirs = (
            await db_session.execute(
                text("SELECT department FROM employee_profiles WHERE user_id = :u"),
                {"u": str(colleague)},
            )
        ).scalar()
        assert theirs == "Untouched"

    async def test_another_tenants_profile_is_invisible(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Row-level security, asserted through the service rather than argued for."""
        who = await register_owner(client, mailbox)
        other_org = uuid.uuid4()
        await scope(db_session, other_org)
        await db_session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'other')"), {"i": other_org}
        )
        stranger = await add_user(db_session, other_org, "x@other.example")
        await db_session.execute(
            text(
                "INSERT INTO employee_profiles (user_id, org_id, department) "
                "VALUES (:u, :o, 'Other Tenant')"
            ),
            {"u": stranger, "o": other_org},
        )
        await db_session.commit()

        # Scoped to the caller's organisation, asking for the stranger's id: the policy
        # must hide the row even though the id is correct.
        await scope(db_session, who["org_id"])
        with pytest.raises(NotFound):
            await read_profile(db_session, user_id=stranger)

    async def test_a_write_cannot_land_in_another_tenant(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """`org_id` comes from the GUC inside the statement, never from Python.

        Scoped to org A while writing for a user who belongs to org B, the composite
        foreign key and the policy's WITH CHECK clause both refuse — the row cannot be
        created under the wrong tenant.
        """
        who = await register_owner(client, mailbox)
        other_org = uuid.uuid4()
        await scope(db_session, other_org)
        await db_session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'other')"), {"i": other_org}
        )
        stranger = await add_user(db_session, other_org, "y@other.example")
        await db_session.commit()

        await scope(db_session, who["org_id"])
        with pytest.raises(Exception):  # noqa: B017 - FK or RLS, both are correct refusals
            await upsert_profile(
                db_session,
                user_id=stranger,
                update=ProfileUpdate(
                    values={"department": "X"}, provided=frozenset({"department"})
                ),
            )
        await db_session.rollback()


# ------------------------------------------------------------------------ the contract


class TestTheGeneratedContract:
    def test_the_route_declares_both_permissions(self) -> None:
        from jutsu_api.routers.me import read_my_profile, update_my_profile
        from jutsu_api.security import declaration_of
        from jutsu_core.rbac import Permission

        read = declaration_of(read_my_profile)
        write = declaration_of(update_my_profile)
        assert read is not None and write is not None
        assert read.permission is Permission.PROFILE_SELF_READ
        assert write.permission is Permission.PROFILE_SELF_UPDATE

    def test_both_permissions_are_held_by_every_role(self) -> None:
        """Otherwise a bare Member could not read or complete their own profile."""
        from jutsu_core.rbac import ROLE_PERMISSIONS, Permission

        for role, permissions in ROLE_PERMISSIONS.items():
            assert Permission.PROFILE_SELF_READ in permissions, role
            assert Permission.PROFILE_SELF_UPDATE in permissions, role

    def test_the_openapi_document_matches_the_implementation(self) -> None:
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        document = json.loads((root / "apps/web/lib/openapi.json").read_text(encoding="utf-8"))

        assert "/v1/me/profile" in document["paths"]
        assert set(document["paths"]["/v1/me/profile"]) >= {"get", "patch"}

        patch = document["components"]["schemas"]["ProfilePatch"]
        # The generated client must refuse unknown fields too, or `extra="forbid"` is a
        # server-side rule the browser never learns about.
        assert patch["additionalProperties"] is False
        assert "user_id" not in patch["properties"]
        assert "org_id" not in patch["properties"]

        view = document["components"]["schemas"]["ProfileView"]
        assert "user_id" not in view["properties"]
        assert "org_id" not in view["properties"]
