"""The KT handover, A to B, over the wire against real Postgres, RLS and the audit trail.

The business flow in one sentence: an administrator creates a package for Employee A, the
system issues its KT ID, and Employee B, signed into B's own console in the same
organisation, enters that ID and gets a workspace scoped to the package, and nothing else
of A's.

The production report behind this file was "B enters A's KT ID and is told no package
matches it". The server-side trail showed two different refusals behind that one sentence,
identical to the caller by design (ADR 0016 §2): a caller whose session was in another
organisation, where the code does not exist, and a package already bound to somebody else
by the time B arrived. The first is the tenant boundary working, and `TestTenantIsolation`
pins it. The second is the first-claim rule working, except for one claimer it should never
have accepted: the package's own subject, who could bind it to themselves for good.
`test_the_subject_opening_first_no_longer_locks_the_recipient_out` is that sequence, and it
fails against the code before the fix.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender, get_object_store
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_db.engine import dispose_engine
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

OTHER_REGISTRATION = {
    "full_name": "Carol Shaw",
    "work_email": "carol@othersystems.com",
    "company_name": "Other Systems",
    "company_domain": "othersystems.com",
    "job_title": "Chief Engineer",
    "org_size": "11-50",
    "terms_accepted": True,
}

OWNER_EMAIL = "ada@example.com"
#: Employee A — the package's subject, whose context is being handed over.
LEAVER = "leaver@example.com"
#: Employee B — taking over.
SUCCESSOR = "successor@example.com"
#: Employee C — same organisation, nothing to do with this handover.
BYSTANDER = "bystander@example.com"

NOT_FOUND = "No package matches that ID. Check it with your administrator."
UNKNOWN_CODE = "KT-JUTSU-00000000"


class FakeStore:
    """A bucket in a dict, recording every key it was asked to sign (see test_kt_files)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed_downloads: list[tuple[str, str]] = []

    def signed_upload(self, key: str, *, content_type: str, max_bytes: int) -> Any:
        from jutsu_core.storage import SignedUpload

        return SignedUpload(
            url=f"https://storage.example/{key}",
            headers={
                "Content-Type": content_type,
                "x-goog-content-length-range": f"0,{max_bytes}",
            },
            expires_in_seconds=1800,
        )

    def signed_download(self, key: str, *, filename: str) -> str:
        self.signed_downloads.append((key, filename))
        return f"https://storage.example/{key}?download={filename}"

    def stat(self, key: str) -> tuple[int, str] | None:
        blob = self.objects.get(key)
        return (len(blob), "fakecrc32c") if blob is not None else None

    def read_head(self, key: str, *, count: int = 4096) -> bytes:
        return self.objects.get(key, b"")[:count]

    def download(self, key: str, *, max_bytes: int) -> bytes:
        return self.objects.get(key, b"")[:max_bytes]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    store: FakeStore,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """The app, with the denied-open audit writer's own engine on the application role.

    Denied opens commit on their own session through `jutsu_db.engine`, so `DATABASE_URL`
    must name the application role (`db_session` leaves it on the privileged migration
    URL, under which the tenant assertions below would pass vacuously), and the cached
    engine is disposed around every test. test_kt.py documents both.
    """
    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_object_store] = lambda: store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http

    await dispose_engine()


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def register(
    client: AsyncClient, mailbox: RecordingEmailSender, registration: dict[str, Any]
) -> None:
    started = await client.post("/v1/orgs/register", json=registration)
    assert started.status_code == 202, started.text
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text


async def invite_and_accept(
    client: AsyncClient, mailbox: RecordingEmailSender, *, email: str, full_name: str
) -> None:
    """Invite as the signed-in administrator; accepting signs the client in as the invitee."""
    invited = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": "member"},
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
    recipient_email: str | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject_user_id": subject_user_id,
        "scope": scope or ["documents", "profile"],
        "validity_days": 30,
    }
    if recipient_email:
        payload["recipient_email"] = recipient_email
    response = await client.post("/v1/kt", json=payload, headers=csrf(client))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def claim(client: AsyncClient, kt_code: str) -> Response:
    """The console's own door: the CSRF-checked POST that `KtShell` and /handover call."""
    return await client.post("/v1/kt/claim", json={"kt_code": kt_code}, headers=csrf(client))


async def handover(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    *,
    recipient_email: str | None = None,
    scope: list[str] | None = None,
) -> dict[str, Any]:
    """The standing setup: an owner, Employee A and Employee B, and A's package.

    Leaves the client signed in as the owner, and returns the administrator's view of the
    package — the KT ID the owner would share, plus the id no recipient ever sees.
    """
    await register(client, mailbox, REGISTRATION)
    await invite_and_accept(client, mailbox, email=LEAVER, full_name="Grace Hopper")
    await sign_in(client, mailbox, email=OWNER_EMAIL)
    await invite_and_accept(client, mailbox, email=SUCCESSOR, full_name="Barbara Liskov")
    await sign_in(client, mailbox, email=OWNER_EMAIL)
    subject = await user_id_of(client, LEAVER)
    return await create_kt(
        client, subject_user_id=subject, recipient_email=recipient_email, scope=scope
    )


async def upload(client: AsyncClient, store: FakeStore, *, filename: str) -> dict[str, Any]:
    """One text file into the signed-in employee's own basket, completed."""
    body = f"What {filename} says, in plain text.".encode()
    started = await client.post(
        "/v1/basket/files",
        json={"filename": filename, "content_type": "text/plain", "size_bytes": len(body)},
        headers=csrf(client),
    )
    assert started.status_code == 201, started.text
    ticket = started.json()
    store.objects[ticket["url"].removeprefix("https://storage.example/")] = body
    done = await client.post(f"/v1/basket/files/{ticket['file_id']}/complete", headers=csrf(client))
    assert done.status_code == 200, done.text
    return dict(done.json())


async def bound_to(inspector: AsyncSession, package_id: object) -> str | None:
    """Who the package is bound to, read as the owner so RLS cannot hide the answer."""
    row = (
        await inspector.execute(
            text("SELECT recipient_user_id FROM kt_packages WHERE id = :id"),
            {"id": str(package_id)},
        )
    ).one()
    return None if row.recipient_user_id is None else str(row.recipient_user_id)


async def denied_reasons(inspector: AsyncSession) -> list[tuple[str, str]]:
    """Every refused open as `(org_id, reason)`, in the order they were written."""
    rows = (
        await inspector.execute(
            text(
                "SELECT org_id, meta_json ->> 'reason' AS reason FROM audit_log "
                "WHERE action = 'kt.open' AND outcome = 'denied' ORDER BY id"
            )
        )
    ).all()
    return [(str(r.org_id), r.reason) for r in rows]


class TestTheHandover:
    async def test_b_opens_an_unaddressed_package_created_for_a(
        self, client: AsyncClient, mailbox: RecordingEmailSender, inspector: AsyncSession
    ) -> None:
        """Model A, the bearer case: nobody addressed, so the first eligible opener binds."""
        package = await handover(client, mailbox)

        await sign_in(client, mailbox, email=SUCCESSOR)
        opened = await claim(client, package["kt_code"])

        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["kt_code"] == package["kt_code"]
        assert body["status"] == "claimed"
        assert body["subject"]["display_name"] == "Grace Hopper"
        # The door opens onto a workspace that answers, not onto a dead end.
        assert (await client.get(f"/v1/kt/{package['kt_code']}/documents")).status_code == 200

        successor = (await client.get("/v1/me")).json()["user_id"]
        assert await bound_to(inspector, package["id"]) == successor

    async def test_the_subject_opening_first_no_longer_locks_the_recipient_out(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
    ) -> None:
        """The production sequence: A holds A's own KT ID and opens it before B does.

        Before the fix, the subject's open bound the package to the subject, permanently —
        a claimed package cannot be re-addressed — and B got the uniform 404 for ever.
        """
        package = await handover(client, mailbox)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        await sign_in(client, mailbox, email=LEAVER)
        refused = await claim(client, package["kt_code"])
        assert refused.status_code == 404
        assert refused.json()["error"]["message"] == NOT_FOUND
        await db_session.rollback()
        assert await bound_to(inspector, package["id"]) is None, "the subject's open bound it"

        await sign_in(client, mailbox, email=SUCCESSOR)
        opened = await claim(client, package["kt_code"])
        assert opened.status_code == 200, opened.text

        # The operator can still tell this refusal from a typo; the caller cannot.
        assert await denied_reasons(inspector) == [(org_id, "subject_of_package")]

    async def test_the_subjects_refusal_is_the_uniform_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The subject is told nothing a typo is not told — not that the package exists,
        not that it is about them, and not which rule refused."""
        package = await handover(client, mailbox)
        await sign_in(client, mailbox, email=LEAVER)

        own = await claim(client, package["kt_code"])
        typo = await claim(client, UNKNOWN_CODE)

        assert own.status_code == typo.status_code == 404
        assert own.json()["error"]["code"] == typo.json()["error"]["code"]
        assert own.json()["error"]["message"] == typo.json()["error"]["message"] == NOT_FOUND
        assert "subject" not in own.text

    async def test_a_package_cannot_be_handed_to_its_own_subject(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Addressing the leaver's own account is refused where the mistake is made.

        The subject can never claim the package, so one addressed to them could never be
        opened by anybody, and the colleague it was for would get the uniform 404 with
        nothing to say why. Creation and re-addressing both refuse it, whatever the case of
        the address, and the package stays re-addressable to the person taking over.
        """
        refusal = (
            "A package can't be handed over to the employee it is about. "
            "Choose the colleague who is taking over."
        )
        package = await handover(client, mailbox)
        subject = await user_id_of(client, LEAVER)

        created = await client.post(
            "/v1/kt",
            json={
                "subject_user_id": subject,
                "scope": ["documents"],
                "validity_days": 30,
                "recipient_email": LEAVER.upper(),
            },
            headers=csrf(client),
        )
        assert created.status_code == 422, created.text
        assert created.json()["error"]["message"] == refusal

        readdressed = await client.patch(
            f"/v1/kt/{package['id']}", json={"recipient_email": LEAVER}, headers=csrf(client)
        )
        assert readdressed.status_code == 422, readdressed.text
        assert readdressed.json()["error"]["message"] == refusal

        handed = await client.patch(
            f"/v1/kt/{package['id']}", json={"recipient_email": SUCCESSOR}, headers=csrf(client)
        )
        assert handed.status_code == 200, handed.text
        assert handed.json()["recipient_email"] == SUCCESSOR

        await sign_in(client, mailbox, email=SUCCESSOR)
        assert (await claim(client, package["kt_code"])).status_code == 200


class TestWhereYouAreSignedIn:
    async def test_a_member_is_told_which_organisation_they_are_signed_into(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """The KT entry names the session's organisation. The production "B cannot open A's
        package" was a session in another organisation, where the ID does not exist, and
        nothing on the page said so."""
        await handover(client, mailbox)
        await sign_in(client, mailbox, email=SUCCESSOR)

        home = await client.get("/v1/me/organisation")
        assert home.status_code == 200, home.text
        assert home.json() == {"name": "Example Analytical"}

        # Another tenant's session reads its own name, and only its own.
        await register(client, mailbox, OTHER_REGISTRATION)
        assert (await client.get("/v1/me/organisation")).json() == {"name": "Other Systems"}

    async def test_it_needs_a_session(self, client: AsyncClient) -> None:
        assert (await client.get("/v1/me/organisation")).status_code == 401


class TestAddressedPackages:
    async def test_only_the_addressed_recipient_opens_it(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
    ) -> None:
        """Model B: addressed to B. B opens it; C and A get the uniform 404 before B's
        claim and C still does after it."""
        package = await handover(client, mailbox, recipient_email=SUCCESSOR)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        await invite_and_accept(client, mailbox, email=BYSTANDER, full_name="Carol Shaw")

        early = await claim(client, package["kt_code"])
        assert early.status_code == 404
        assert early.json()["error"]["message"] == NOT_FOUND

        await sign_in(client, mailbox, email=LEAVER)
        assert (await claim(client, package["kt_code"])).status_code == 404

        await sign_in(client, mailbox, email=SUCCESSOR)
        assert (await claim(client, package["kt_code"])).status_code == 200

        await sign_in(client, mailbox, email=BYSTANDER)
        late = await claim(client, package["kt_code"])
        assert late.status_code == 404
        assert late.json()["error"]["message"] == NOT_FOUND

        await db_session.rollback()
        assert await denied_reasons(inspector) == [
            (org_id, "addressed_to_another_email"),  # C, before anyone opened it
            (org_id, "addressed_to_another_email"),  # A: addressed elsewhere decides first
            (org_id, "bound_to_another_user"),  # C, after B's claim
        ]


class TestRefusals:
    async def test_an_unknown_code_is_refused_without_detail(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register(client, mailbox, REGISTRATION)
        org_id = (await client.get("/v1/orgs/current")).json()["id"]

        refused = await claim(client, UNKNOWN_CODE)

        assert refused.status_code == 404
        assert refused.json()["error"]["message"] == NOT_FOUND
        for internal in ("unknown_code", org_id, "recipient", "subject", "kt_packages"):
            assert internal not in refused.text

    @pytest.mark.parametrize(
        ("close", "sentence"),
        [
            ("expire", "This Knowledge Transfer package has expired."),
            ("revoke", "This Knowledge Transfer package has been revoked."),
            (
                "complete",
                "This Knowledge Transfer is complete. "
                "Ask your administrator if you need it reopened.",
            ),
        ],
    )
    async def test_a_closed_package_is_refused_with_its_own_sentence(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
        close: str,
        sentence: str,
    ) -> None:
        """§39: the eligible holder is told exactly why the door is shut, in the
        application's own sentence — which names no id, no address and no rule — and
        nothing binds on the way."""
        package = await handover(client, mailbox)
        if close == "expire":
            org_id = (await client.get("/v1/orgs/current")).json()["id"]
            await db_session.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
            )
            await db_session.execute(
                text(
                    "UPDATE kt_packages SET expires_at = now() - interval '1 hour' WHERE id = :id"
                ),
                {"id": package["id"]},
            )
        else:
            closed = await client.post(f"/v1/kt/{package['id']}/{close}", headers=csrf(client))
            assert closed.status_code == 200, closed.text

        await sign_in(client, mailbox, email=SUCCESSOR)
        refused = await claim(client, package["kt_code"])

        assert refused.status_code == 403
        assert refused.json()["error"]["message"] == sentence
        assert str(package["id"]) not in refused.text
        await db_session.rollback()
        assert await bound_to(inspector, package["id"]) is None


class TestTenantIsolation:
    async def test_a_member_of_another_organisation_gets_the_unknown_code_404(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        inspector: AsyncSession,
    ) -> None:
        """The production `unknown_code` case: the caller's session is in another tenant.

        The code is looked up under the caller's own organisation, so from outside it is
        exactly a typo — same status, same code, same sentence — the refusal is recorded
        in the caller's tenant, and nothing about the real package moves.
        """
        package = await handover(client, mailbox)
        home_org = (await client.get("/v1/orgs/current")).json()["id"]

        await register(client, mailbox, OTHER_REGISTRATION)
        other_org = (await client.get("/v1/orgs/current")).json()["id"]
        assert other_org != home_org

        foreign = await claim(client, package["kt_code"])
        typo = await claim(client, UNKNOWN_CODE)

        assert foreign.status_code == typo.status_code == 404
        assert foreign.json()["error"]["code"] == typo.json()["error"]["code"]
        assert foreign.json()["error"]["message"] == typo.json()["error"]["message"]
        for internal in (home_org, str(package["id"]), "Grace Hopper"):
            assert internal not in foreign.text

        await db_session.rollback()
        assert await bound_to(inspector, package["id"]) is None
        assert await denied_reasons(inspector) == [
            (other_org, "unknown_code"),
            (other_org, "unknown_code"),
        ]


class TestPackageScope:
    async def test_the_recipient_reaches_the_attached_file_and_nothing_else_of_as(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """ADR 0021 from B's side. A attaches exactly one of A's two basket files. B, holding
        the package, lists that one and downloads that one, and cannot turn the other file's
        id into bytes, with a valid session and a valid code."""
        await register(client, mailbox, REGISTRATION)
        await invite_and_accept(client, mailbox, email=LEAVER, full_name="Grace Hopper")
        shared = await upload(client, store, filename="runbook.txt")
        private = await upload(client, store, filename="salary-review.txt")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        await invite_and_accept(client, mailbox, email=SUCCESSOR, full_name="Barbara Liskov")
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        package = await create_kt(client, subject_user_id=await user_id_of(client, LEAVER))

        # The subject curating their own handover (ADR 0021) is not an open, so the rule
        # that the subject may never claim the package must leave it alone.
        await sign_in(client, mailbox, email=LEAVER)
        attached = await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [shared["id"]]},
            headers=csrf(client),
        )
        assert attached.status_code == 201, attached.text
        assert attached.json()["attached"] == 1

        await sign_in(client, mailbox, email=SUCCESSOR)
        assert (await claim(client, package["kt_code"])).status_code == 200

        listing = await client.get(f"/v1/kt/{package['kt_code']}/files")
        assert listing.status_code == 200, listing.text
        assert [f["filename"] for f in listing.json()["items"]] == ["runbook.txt"]
        assert "salary-review" not in listing.text

        allowed = await client.get(f"/v1/kt/{package['kt_code']}/files/{shared['id']}/download")
        assert allowed.status_code == 200, allowed.text
        refused = await client.get(f"/v1/kt/{package['kt_code']}/files/{private['id']}/download")
        assert refused.status_code == 404
        assert not any(str(private["id"]) in key for key, _ in store.signed_downloads)

        # And A's basket as a whole is still A's alone: B's own listing holds neither file.
        mine = await client.get("/v1/basket/files")
        assert mine.status_code == 200, mine.text
        assert "runbook" not in mine.text
        assert "salary-review" not in mine.text

    async def test_the_recipient_view_carries_nothing_private_of_the_subjects(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        """Owner privacy: knowing A's KT ID gets B the package's view of A — a name, and
        profile fields only under the `profile` scope — never A's address, ids, tenant or
        identities, and claiming grants B no principal of A's."""
        package = await handover(client, mailbox, scope=["documents"])
        org_id = (await client.get("/v1/orgs/current")).json()["id"]
        subject_id = await user_id_of(client, LEAVER)

        await sign_in(client, mailbox, email=SUCCESSOR)
        opened = await claim(client, package["kt_code"])
        assert opened.status_code == 200, opened.text
        body = opened.json()

        assert set(body) == {
            "kt_code",
            "status",
            "scope",
            "period_start",
            "period_end",
            "expires_at",
            "created_at",
            "subject",
        }
        assert body["subject"]["display_name"] == "Grace Hopper"
        # No `profile` scope: a name, and every other profile field empty.
        assert all(value is None for key, value in body["subject"].items() if key != "display_name")
        for private in (LEAVER, OWNER_EMAIL, subject_id, org_id, str(package["id"])):
            assert private not in opened.text

        identities = await client.get("/v1/me/identities")
        assert identities.status_code == 200, identities.text
        assert LEAVER not in identities.text
