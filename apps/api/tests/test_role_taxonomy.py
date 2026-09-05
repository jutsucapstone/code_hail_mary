"""The role taxonomy over the wire, against real Postgres and real RLS.

Three things are being proved here, and none of them can be proved with a mock:

* **The catalogue is honest.** Both source documents are represented, ambiguity survives
  the round trip, and the one level with no platform code still says so.
* **Standing is not authority.** A role code confers nothing, the four governance seats
  need rank on top of permission, and nobody may seat themselves in one.
* **The tenant boundary holds.** Row-level security decides who is even visible, so a
  cross-tenant assignment is a 404 rather than a refusal — the two are indistinguishable
  to the caller, which is the point.

The adversarial cases (§23 of the brief) are not a separate file. A test that only ever
sends well-formed input from an authorised caller proves the happy path and nothing about
the boundary, so the refusals live beside the successes they mirror.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http


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
    full_name: str = "Charles Babbage",
    role: str = "member",
) -> None:
    """Invite and accept. NOTE: leaves the client signed in as the invitee."""
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
    requested = await client.post("/v1/auth/request", json={"email": email})
    assert requested.status_code == 202, requested.text
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def user_id_of(client: AsyncClient, email: str) -> str:
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def assign(
    client: AsyncClient, target: str, **fields: object
) -> tuple[int, dict[str, object]]:
    return await post_assignment(client, target, dict(fields))


async def post_assignment(
    client: AsyncClient, target: str, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    """Send an arbitrary body, including keys that collide with helper parameters.

    `assign(**fields)` cannot express `{"user_id": ...}` — Python binds it to the
    helper's own argument — and that is exactly the field the smuggling test needs to
    send.
    """
    response = await client.patch(
        f"/v1/employees/{target}/role-assignment", json=payload, headers=csrf(client)
    )
    body = response.json() if response.content else {}
    return response.status_code, body


async def scope_to_caller(client: AsyncClient, session: AsyncSession) -> None:
    """Set `app.current_org_id` on the test's own session before reading a scoped table.

    The GUC is transaction-local, so once the request has committed a raw query from the
    test runs with it unset — and the policy then correctly hides every row. Tests that
    read `audit_log` directly have to re-enter the tenant the same way the request did.
    """
    org_id = (await client.get("/v1/me")).json()["org_id"]
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
    )


SENIOR_SWE = {
    "practice_key": "technology",
    "role_title_key": "senior_software_engineer",
    "role_level_key": "senior_consultant",
    "role_code": "SCN",
}


# --------------------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------------------


class TestCatalogue:
    async def test_a_bare_member_may_read_it(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Gated on `profile:self_read`, which every role holds. An employee who cannot
        read the catalogue cannot be shown the NAME of their own seniority — the page
        would have the key `senior_consultant` and nothing to render."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="member@example.com")

        response = await client.get("/v1/role-catalogue")
        assert response.status_code == 200, response.text

    async def test_both_source_documents_are_represented(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        assert len(body["codes"]) == 11, "eleven JUTSU platform codes"
        assert len(body["practices"]) == 5
        assert len(body["titles"]) == 41
        assert len(body["levels"]) == 12
        assert {code["code"] for code in body["codes"]} == {
            "CHM",
            "CEO",
            "ITA",
            "HRA",
            "PTR",
            "SMR",
            "MGR",
            "AMR",
            "SCN",
            "CON",
            "ANS",
        }

    async def test_exactly_the_four_governance_seats_are_flagged(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        privileged = {code["code"] for code in body["codes"] if code["privileged"]}
        assert privileged == {"CHM", "CEO", "ITA", "HRA"}

    async def test_tiers_match_the_source_document(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        tiers = {code["code"]: code["tier"] for code in body["codes"]}
        assert tiers == {
            "CHM": 8,
            "CEO": 7,
            "ITA": 6,
            "HRA": 6,
            "PTR": 6,
            "SMR": 5,
            "MGR": 4,
            "AMR": 3,
            "SCN": 2,
            "CON": 1,
            "ANS": 1,
        }

    async def test_an_ambiguous_title_keeps_both_levels(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """ "Audit Senior → Consultant / Senior Consultant" is the source being genuinely
        ambiguous. Collapsing it to one would be this system inventing a fact."""
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        audit_senior = next(t for t in body["titles"] if t["key"] == "audit_senior")
        assert set(audit_senior["level_keys"]) == {"consultant", "senior_consultant"}
        assert audit_senior["default_level_key"] == "consultant", "the source lists it first"

    async def test_director_has_no_suggested_platform_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The JUTSU catalogue jumps SMR (T5) to PTR (T6). Suggesting either would invent
        a promotion or a demotion, so the gap is reported as null."""
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        director = next(level for level in body["levels"] if level["key"] == "director")
        assert director["suggested_code"] is None

    async def test_no_level_suggests_a_governance_seat(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """A Manager is not an IT Admin Controller because both happen to be senior."""
        await register_owner(client, mailbox)
        body = (await client.get("/v1/role-catalogue")).json()

        suggested = {level["suggested_code"] for level in body["levels"]}
        assert not suggested & {"CHM", "CEO", "ITA", "HRA"}


# --------------------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------------------


class TestAssignment:
    async def test_an_owner_assigns_the_whole_taxonomy(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        status, body = await assign(client, user_id, **SENIOR_SWE)

        assert status == 200, body
        assert body["role_title"] == "Senior Software Engineer"
        assert body["role_level"] == "Senior Consultant"
        assert body["practice"] == "Technology"
        assert body["discipline"] == "Software Engineering"
        assert body["role_code"] == "SCN"
        assert body["mapping_status"] == "mapped"

    async def test_the_change_is_audited_with_before_and_after(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        await assign(client, user_id, **SENIOR_SWE)

        await scope_to_caller(client, db_session)
        row = (
            await db_session.execute(
                text(
                    "SELECT meta_json FROM audit_log "
                    "WHERE action = 'member.role_taxonomy_changed' ORDER BY ts DESC LIMIT 1"
                )
            )
        ).scalar_one()
        changes = row["changes"]
        assert changes["role_code"] == {"from": None, "to": "SCN"}
        assert changes["role_level_key"]["to"] == "senior_consultant"

    async def test_the_status_is_derived_not_accepted(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """A client that could send `role_mapping_status` could claim `mapped` over an
        empty row. The field is not on the model and `extra="forbid"` refuses it."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        status, _ = await assign(client, user_id, role_mapping_status="mapped")
        assert status == 422

    async def test_the_custom_path_keeps_a_normalized_level(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """An organisation whose vocabulary the catalogue does not carry still stays
        comparable, because a custom title with no level is invisible to every
        cross-practice query."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        status, body = await assign(
            client,
            user_id,
            role_title_custom="Chief Remote Sensing Officer",
            role_level_key="manager",
        )
        assert status == 200, body
        assert body["mapping_status"] == "custom"
        assert body["role_title"] == "Chief Remote Sensing Officer"
        assert body["role_level"] == "Manager"

    async def test_assignment_leaves_the_free_text_designation_alone(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`designation` keeps its old meaning. The taxonomy is added beside it, not
        over it — nothing here rewrites what somebody called themselves."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await client.patch(
            "/v1/me/profile",
            json={"designation": "Sr. SWE (self-described)"},
            headers=csrf(client),
        )
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await assign(client, user_id, **SENIOR_SWE)

        await sign_in(client, mailbox, email="dev@example.com")
        profile = (await client.get("/v1/me/profile")).json()
        assert profile["designation"] == "Sr. SWE (self-described)"
        assert profile["role"]["role_title"] == "Senior Software Engineer"


# --------------------------------------------------------------------------------------
# Validation — §18
# --------------------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize(
        ("fields", "because"),
        [
            (
                {
                    "practice_key": "technology",
                    "role_title_key": "tax_consultant_i",
                    "role_level_key": "consultant",
                },
                "Tax Consultant I is not a Technology title",
            ),
            (
                {
                    "practice_key": "technology",
                    "role_title_key": "software_engineer",
                    "role_level_key": "partner",
                },
                "the catalogue does not admit Partner for a Software Engineer",
            ),
            (
                {
                    "practice_key": "technology",
                    "role_title_key": "not_a_real_title",
                    "role_level_key": "consultant",
                },
                "unknown title",
            ),
            (
                {"role_code": "SUPERADMIN"},
                "unknown platform role code",
            ),
            (
                {"role_title_custom": "Chief Vibes Officer"},
                "a custom title with no level is invisible to search",
            ),
            (
                {"role_level_key": "manager"},
                "a level with no title to belong to",
            ),
        ],
    )
    async def test_impossible_combinations_are_refused(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        fields: dict[str, object],
        because: str,
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        status, body = await assign(client, user_id, **fields)
        assert status == 422, f"{because}: got {status} {body}"

    async def test_the_second_admitted_level_is_accepted(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Ambiguity is preserved, so BOTH of an ambiguous title's levels are legal —
        the refusal above must not be over-broad."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="auditor@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "auditor@example.com")

        status, body = await assign(
            client,
            user_id,
            practice_key="audit_assurance",
            role_title_key="audit_senior",
            role_level_key="senior_consultant",
        )
        assert status == 200, body
        assert body["role_level"] == "Senior Consultant"

    async def test_identity_bearing_fields_are_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`extra="forbid"` is a security control here. The tenant comes from the
        session and the target from the path; neither is reachable from the body."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        for smuggled in ("org_id", "user_id", "tenant_id", "permissions", "role_key"):
            status, _ = await post_assignment(client, user_id, {smuggled: "anything"})
            assert status == 422, f"{smuggled} must be refused outright"


# --------------------------------------------------------------------------------------
# Authorization — §15, §23
# --------------------------------------------------------------------------------------


class TestAuthorization:
    async def test_a_member_may_not_assign_anything(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await sign_in(client, mailbox, email="dev@example.com")

        status, _ = await assign(client, user_id, **SENIOR_SWE)
        assert status == 403

    async def test_a_member_may_not_promote_themselves(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The whole reason assignment is not part of `PATCH /v1/me/profile`."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await sign_in(client, mailbox, email="dev@example.com")

        status, _ = await assign(
            client,
            user_id,
            practice_key="audit_assurance",
            role_title_key="partner_managing_director",
            role_level_key="partner",
            role_code="PTR",
        )
        assert status == 403

    async def test_a_member_may_not_reach_the_taxonomy_through_their_own_profile(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")

        response = await client.patch(
            "/v1/me/profile", json={"role_code": "CHM"}, headers=csrf(client)
        )
        assert response.status_code == 422

    async def test_an_analyst_who_may_read_people_may_not_assign(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """`member:read` and `member:assign_role_code` are different powers."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="analyst@example.com", role="analyst")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await sign_in(client, mailbox, email="analyst@example.com")

        status, _ = await assign(client, user_id, **SENIOR_SWE)
        assert status == 403

    async def test_hr_admin_may_assign_an_ordinary_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await sign_in(client, mailbox, email="hr@example.com")

        status, body = await assign(client, user_id, **SENIOR_SWE)
        assert status == 200, body
        assert body["role_code"] == "SCN"

    @pytest.mark.parametrize("seat", ["CHM", "CEO", "ITA", "HRA"])
    async def test_hr_admin_may_not_seat_anybody_in_a_governance_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender, seat: str
    ) -> None:
        """HR holds `member:assign_role_code`, and the four governance seats are still
        not HR's to hand out. Rank on top of permission."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await sign_in(client, mailbox, email="hr@example.com")

        status, _ = await assign(client, user_id, role_code=seat)
        assert status == 403, f"hr_admin must not be able to seat anybody as {seat}"

    async def test_hr_admin_may_not_strip_a_governance_code_either(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The other direction of the same rule.

        Guarding only the grant would let an HR Admin vacate a Chairman's seat while
        being unable to award one — the same integrity problem seen from the other end,
        and one §16 wants recorded as a privileged removal rather than done quietly.
        """
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        seated, _ = await assign(client, user_id, role_code="ITA")
        assert seated == 200

        await sign_in(client, mailbox, email="hr@example.com")
        status, _ = await post_assignment(client, user_id, {"role_code": None})

        assert status == 403, "vacating a governance seat needs the same rank as filling it"

    async def test_an_owner_may_vacate_a_governance_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The refusal above must not be over-broad: an Owner still can."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await assign(client, user_id, role_code="ITA")

        status, body = await post_assignment(client, user_id, {"role_code": None})

        assert status == 200, body
        assert body["role_code"] is None

    async def test_hr_admin_may_still_change_business_fields_of_a_seated_person(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """And must not be locked out of ordinary work by the guard above: the rule is
        about the CODE changing, not about the person holding one."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email="hr@example.com", role="hr_admin")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        await assign(client, user_id, role_code="ITA")

        await sign_in(client, mailbox, email="hr@example.com")
        status, body = await post_assignment(
            client,
            user_id,
            {
                "practice_key": "technology",
                "role_title_key": "senior_software_engineer",
                "role_level_key": "senior_consultant",
            },
        )

        assert status == 200, body
        assert body["role_code"] == "ITA", "the seat is untouched"
        assert body["role_title"] == "Senior Software Engineer"

    async def test_an_owner_may_seat_a_governance_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")

        status, body = await assign(client, user_id, role_code="ITA")
        assert status == 200, body
        assert body["role_code"] == "ITA"

    async def test_nobody_seats_themselves_in_a_governance_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Not an escalation guard — codes confer nothing — but an integrity one. A
        Chairman is a fact about the organisation, not a self-description."""
        await register_owner(client, mailbox)
        own_id = await user_id_of(client, OWNER_EMAIL)

        status, _ = await assign(client, own_id, role_code="CHM")
        assert status == 403

    async def test_a_role_code_grants_no_permission(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The load-bearing separation. `HRA` looks like `hr_admin` and is not: seating a
        bare Member in the HR Admin Controller code must leave them unable to do a single
        thing they could not do before."""
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        user_id = await user_id_of(client, "dev@example.com")
        assigned, _ = await assign(client, user_id, role_code="HRA")
        assert assigned == 200

        await sign_in(client, mailbox, email="dev@example.com")
        identity = (await client.get("/v1/me")).json()
        assert identity["role"] == "member", "the RBAC role is untouched"
        assert (await client.get("/v1/employees")).status_code == 403
        assert (await client.get("/v1/audit")).status_code == 403
        status, _ = await assign(client, user_id, role_code="ANS")
        assert status == 403, "still cannot assign, despite holding the HRA seat"


# --------------------------------------------------------------------------------------
# Tenant isolation — §14
# --------------------------------------------------------------------------------------


OTHER_REGISTRATION = {
    "full_name": "Grace Hopper",
    "work_email": "grace@othersystems.com",
    "company_name": "Other Systems",
    "company_domain": "othersystems.com",
    "job_title": "Chief Engineer",
    "org_size": "11-50",
    "terms_accepted": True,
}


class TestTenantIsolation:
    async def _second_org(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        started = await client.post("/v1/orgs/register", json=OTHER_REGISTRATION)
        assert started.status_code == 202, started.text
        delivered = mailbox.last.secrets
        verified = await client.post(
            "/v1/orgs/register/verify",
            json={"token": delivered["token"], "code": delivered["code"]},
        )
        assert verified.status_code == 200, verified.text

    async def test_an_admin_cannot_assign_across_tenants(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        victim = await user_id_of(client, "dev@example.com")

        await self._second_org(client, mailbox)
        status, _ = await assign(client, victim, **SENIOR_SWE)

        assert status == 404, "another tenant's employee is not found, not refused"

    async def test_an_admin_cannot_read_another_tenants_assignment(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        victim = await user_id_of(client, "dev@example.com")
        await assign(client, victim, **SENIOR_SWE)

        await self._second_org(client, mailbox)
        response = await client.get(f"/v1/employees/{victim}/role-assignment")

        assert response.status_code == 404

    async def test_the_roster_never_leaks_another_tenant(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="dev@example.com")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await assign(client, await user_id_of(client, "dev@example.com"), **SENIOR_SWE)

        await self._second_org(client, mailbox)
        page = (await client.get("/v1/employees", params={"level": "senior_consultant"})).json()

        assert page["items"] == [], "the filter must not reach across the tenant boundary"


# --------------------------------------------------------------------------------------
# Expert Finder — §11, §13
# --------------------------------------------------------------------------------------


class TestNormalizedSearch:
    async def _seed(self, client: AsyncClient, mailbox: RecordingEmailSender) -> None:
        """Three people at the same normalized seniority whose titles share no word."""
        await register_owner(client, mailbox)
        for email in ("swe@example.com", "auditor@example.com", "tax@example.com"):
            await invite_and_accept(client, mailbox, email=email)
            await sign_in(client, mailbox, email=OWNER_EMAIL)

        await assign(client, await user_id_of(client, "swe@example.com"), **SENIOR_SWE)
        await assign(
            client,
            await user_id_of(client, "auditor@example.com"),
            practice_key="audit_assurance",
            role_title_key="audit_senior",
            role_level_key="senior_consultant",
            role_code="SCN",
        )
        await assign(
            client,
            await user_id_of(client, "tax@example.com"),
            practice_key="tax_legal",
            role_title_key="senior_tax_consultant",
            role_level_key="senior_consultant",
            role_code="SCN",
        )

    async def test_one_level_finds_people_across_practices(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The question the source document says the dashboard must answer."""
        await self._seed(client, mailbox)

        page = (await client.get("/v1/employees", params={"level": "senior_consultant"})).json()

        emails = {item["email"] for item in page["items"]}
        assert emails == {"swe@example.com", "auditor@example.com", "tax@example.com"}

    async def test_the_real_titles_survive_the_normalisation(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Comparing people must not rename them."""
        await self._seed(client, mailbox)

        page = (await client.get("/v1/employees", params={"level": "senior_consultant"})).json()

        titles = {item["email"]: item["role_title"] for item in page["items"]}
        assert titles["swe@example.com"] == "Senior Software Engineer"
        assert titles["auditor@example.com"] == "Audit Senior (Audit In-Charge)"
        assert titles["tax@example.com"] == "Senior Tax Consultant"
        assert all(item["role_level"] == "Senior Consultant" for item in page["items"])

    async def test_a_practice_filter_narrows_to_one_business_line(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._seed(client, mailbox)

        page = (await client.get("/v1/employees", params={"practice": "tax_legal"})).json()

        assert {item["email"] for item in page["items"]} == {"tax@example.com"}

    async def test_a_code_filter_selects_by_platform_standing(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await self._seed(client, mailbox)

        page = (await client.get("/v1/employees", params={"role_code": "SCN"})).json()

        assert len(page["items"]) == 3

    async def test_the_unmapped_filter_is_the_review_queue(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Migration 0018 deliberately left existing people unmapped rather than guessing.
        This is how an administrator finds them."""
        await self._seed(client, mailbox)

        page = (await client.get("/v1/employees", params={"unmapped": "true"})).json()

        emails = {item["email"] for item in page["items"]}
        assert emails == {OWNER_EMAIL}, "only the owner was never assigned"
        assert all(item["mapping_status"] == "unmapped" for item in page["items"])

    async def test_a_person_with_no_profile_reads_as_unmapped(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Legacy data: `LEFT JOIN` plus a coalesce, so somebody who never had a profile
        row is not missing from the roster."""
        await register_owner(client, mailbox)

        page = (await client.get("/v1/employees")).json()

        assert page["items"][0]["mapping_status"] == "unmapped"
        assert page["items"][0]["role_title"] is None
