"""Package-scoped basket sharing over the wire, against real Postgres (ADR 0021).

The claim this file has to earn is that a recipient reaches *exactly* the files attached
to their package and nothing else — not the rest of the subject's basket, not a file that
was detached, not a file whose package was revoked, and not a file in another tenant.

Every one of those is a database question, so the database is real and only the bucket is
fake. A `FakeStore` cannot disagree with the ACL because it never sees one; what it does
give us is the exact key that was signed, which is how "a signed URL was minted for a file
the caller may not read" becomes an assertion rather than an inspection.

The adversarial cases are the point of the file. `TestARecipientReachesNothingElse` is
written from the attacker's side: valid session, valid code, and a file id they should not
be able to turn into bytes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender, get_object_store
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_db.engine import dispose_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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

TEXT_BYTES = b"The migration was decided in March, and here is why."
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeStore:
    """A bucket in a dict. Records every key it was asked to sign."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed_uploads: list[tuple[str, str, int]] = []
        self.signed_downloads: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    def signed_upload(self, key: str, *, content_type: str, max_bytes: int) -> Any:
        from jutsu_core.storage import SignedUpload

        self.signed_uploads.append((key, content_type, max_bytes))
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
        self.deleted.append(key)
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
) -> AsyncIterator[AsyncClient]:
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


async def claim(client: AsyncClient, kt_code: str) -> int:
    """Claim a package the way the console does: through the CSRF-checked POST door.

    A GET can no longer bind an unaddressed package — reading must never decide whose
    package it is, because `verify_csrf` does not check a GET and a link click would
    otherwise hand somebody else's handover to whoever opened it. `KtShell` calls this on
    mount before any panel loads, so every recipient test does the same.

    Returns the status rather than asserting it: the tests that matter here are the ones
    where the claim is REFUSED.
    """
    response = await client.post("/v1/kt/claim", json={"kt_code": kt_code}, headers=csrf(client))
    return response.status_code


async def user_id_of(client: AsyncClient, email: str) -> str:
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def upload(
    client: AsyncClient,
    store: FakeStore,
    *,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
    body: bytes = TEXT_BYTES,
) -> dict[str, Any]:
    started = await client.post(
        "/v1/basket/files",
        json={"filename": filename, "content_type": content_type, "size_bytes": len(body)},
        headers=csrf(client),
    )
    assert started.status_code == 201, started.text
    ticket = started.json()
    store.objects[ticket["url"].removeprefix("https://storage.example/")] = body
    done = await client.post(f"/v1/basket/files/{ticket['file_id']}/complete", headers=csrf(client))
    assert done.status_code == 200, done.text
    return dict(done.json())


async def create_kt(
    client: AsyncClient, *, subject_user_id: str, recipient_email: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject_user_id": subject_user_id,
        "scope": ["documents", "profile"],
        "validity_days": 30,
    }
    if recipient_email:
        payload["recipient_email"] = recipient_email
    response = await client.post("/v1/kt", json=payload, headers=csrf(client))
    assert response.status_code == 201, response.text
    return dict(response.json())


async def a_shared_package(
    client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The whole flow: owner uploads a file, creates a package for herself, attaches it.

    The owner is the subject here so one registration is enough — the sharing boundary
    that matters is between the SUBJECT and the RECIPIENT, and the recipient is a
    separate account in every test that needs one.
    """
    await register_owner(client, mailbox)
    file_row = await upload(client, store)
    owner_id = await user_id_of(client, "ada@example.com")
    package = await create_kt(client, subject_user_id=owner_id)

    attached = await client.post(
        f"/v1/kt/{package['id']}/attachments",
        json={"file_ids": [file_row["id"]]},
        headers=csrf(client),
    )
    assert attached.status_code == 201, attached.text
    assert attached.json()["attached"] == 1
    return package, file_row


# ------------------------------------------------------------------ the happy path


class TestAttachingAndSeeing:
    async def test_a_recipient_sees_a_file_that_was_attached_to_their_package(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        listing = await client.get(f"/v1/kt/{package['kt_code']}/files")

        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert [item["filename"] for item in items] == ["notes.txt"]
        assert items[0]["id"] == file_row["id"]

    async def test_the_recipient_is_told_which_files_are_searchable(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """`stored` and `ready` reach the recipient in the owner's own vocabulary.

        A recipient must not be told a video is searchable, for the same reason the owner
        must not be: nothing in this deployment transcribes one.
        """
        await register_owner(client, mailbox)
        text_file = await upload(client, store)
        image = await upload(
            client, store, filename="whiteboard.png", content_type="image/png", body=PNG_BYTES
        )
        owner_id = await user_id_of(client, "ada@example.com")
        package = await create_kt(client, subject_user_id=owner_id)
        await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [text_file["id"], image["id"]]},
            headers=csrf(client),
        )
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        items = (await client.get(f"/v1/kt/{package['kt_code']}/files")).json()["items"]

        by_name = {item["filename"]: item for item in items}
        assert by_name["whiteboard.png"]["state"] == "stored"
        assert by_name["notes.txt"]["state"] in ("uploaded", "ready")

    async def test_a_recipient_can_download_an_attached_file(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        download = await client.get(f"/v1/kt/{package['kt_code']}/files/{file_row['id']}/download")

        assert download.status_code == 200, download.text
        assert download.json()["url"].startswith("https://storage.example/org/")
        # The key came off the row, never off the request.
        key, filename = store.signed_downloads[-1]
        assert key.endswith(str(file_row["id"]))
        assert filename == "notes.txt"

    async def test_the_recipient_view_never_carries_the_owner_or_the_object_key(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # A recipient is told what the file is. Who owns it and where it lives are the
        # machinery, and the machinery is not theirs.
        package, _ = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        body = (await client.get(f"/v1/kt/{package['kt_code']}/files")).text

        assert "owner_user_id" not in body
        assert "object_key" not in body
        assert "org/" not in body


# ----------------------------------------------------- what a recipient must NOT reach


class TestARecipientReachesNothingElse:
    async def test_an_unattached_file_is_a_404_even_with_a_valid_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """The core claim of ADR 0021, from the attacker's side.

        Valid session, valid package, valid file id belonging to the same subject — and
        it was not attached, so it does not exist as far as this recipient is concerned.
        """
        package, _ = await a_shared_package(client, mailbox, store)
        private = await upload(client, store, filename="salary-review.txt")
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        listing = (await client.get(f"/v1/kt/{package['kt_code']}/files")).json()["items"]
        assert [i["filename"] for i in listing] == ["notes.txt"]

        refused = await client.get(f"/v1/kt/{package['kt_code']}/files/{private['id']}/download")
        assert refused.status_code == 404, refused.text
        # No URL was minted for it — the refusal happened before the store was touched.
        assert not any(str(private["id"]) in key for key, _ in store.signed_downloads)

    async def test_a_detached_file_stops_being_readable(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).json()["items"]

        await sign_in(client, mailbox, email="ada@example.com")
        detached = await client.delete(
            f"/v1/kt/{package['id']}/attachments/{file_row['id']}", headers=csrf(client)
        )
        assert detached.status_code == 204, detached.text

        await sign_in(client, mailbox, email="grace@example.com")
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).json()["items"] == []
        gone = await client.get(f"/v1/kt/{package['kt_code']}/files/{file_row['id']}/download")
        assert gone.status_code == 404

    async def test_revoking_the_package_closes_its_files_with_no_other_action(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """The lifecycle property that made this design worth choosing.

        Nothing runs at revocation time to withdraw file access; `_open_for` refuses and
        the join never happens. If this test fails, the grant has escaped the package.
        """
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).status_code == 200

        await sign_in(client, mailbox, email="ada@example.com")
        revoked = await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))
        assert revoked.status_code == 200, revoked.text

        await sign_in(client, mailbox, email="grace@example.com")
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).status_code == 403
        blocked = await client.get(f"/v1/kt/{package['kt_code']}/files/{file_row['id']}/download")
        assert blocked.status_code == 403

    async def test_completing_the_package_closes_its_files_too(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        package, _ = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).status_code == 200

        await sign_in(client, mailbox, email="ada@example.com")
        assert (
            await client.post(f"/v1/kt/{package['id']}/complete", headers=csrf(client))
        ).status_code == 200

        await sign_in(client, mailbox, email="grace@example.com")
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).status_code == 403

    async def test_the_owner_deleting_a_file_withdraws_it_from_every_package(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # The owner's control over their own file outranks the attachment. Attaching is
        # not a way to pin somebody's file open.
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        # Accepting an invitation signs the client in as the invitee, so the owner has to
        # come back before deleting her own file.
        await sign_in(client, mailbox, email="ada@example.com")

        deleted = await client.delete(f"/v1/basket/files/{file_row['id']}", headers=csrf(client))
        assert deleted.status_code == 204, deleted.text

        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200
        assert (await client.get(f"/v1/kt/{package['kt_code']}/files")).json()["items"] == []
        assert (
            await client.get(f"/v1/kt/{package['kt_code']}/files/{file_row['id']}/download")
        ).status_code == 404

    async def test_a_wrong_recipient_gets_the_same_404_as_a_bad_code(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """Binding before state, inherited from `_open_for` rather than re-implemented."""
        await register_owner(client, mailbox)
        file_row = await upload(client, store)
        owner_id = await user_id_of(client, "ada@example.com")
        package = await create_kt(
            client, subject_user_id=owner_id, recipient_email="grace@example.com"
        )
        await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [file_row["id"]]},
            headers=csrf(client),
        )
        await invite_and_accept(client, mailbox, email="grace@example.com")
        # Grace is now the signed-in principal and cannot invite; the owner has to.
        await sign_in(client, mailbox, email="ada@example.com")
        await invite_and_accept(
            client, mailbox, email="mallory@example.com", full_name="Mallory Byte"
        )
        await sign_in(client, mailbox, email="mallory@example.com")

        refused = await client.get(f"/v1/kt/{package['kt_code']}/files")

        assert refused.status_code == 404, refused.text
        assert "notes.txt" not in refused.text


# ------------------------------------------------------------ who may curate a package


class TestWhoMayAttach:
    async def test_a_plain_member_cannot_curate_somebody_elses_package(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """And gets a 404, not a 403 — nothing confirms the package exists."""
        package, file_row = await a_shared_package(client, mailbox, store)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        assert await claim(client, package["kt_code"]) == 200

        for response in (
            await client.get(f"/v1/kt/{package['id']}/attachments"),
            await client.get(f"/v1/kt/{package['id']}/attachable"),
            await client.post(
                f"/v1/kt/{package['id']}/attachments",
                json={"file_ids": [file_row["id"]]},
                headers=csrf(client),
            ),
            await client.delete(
                f"/v1/kt/{package['id']}/attachments/{file_row['id']}", headers=csrf(client)
            ),
        ):
            assert response.status_code == 404, response.text

    async def test_a_file_belonging_to_somebody_else_is_not_attachable(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """A handover is one person's knowledge.

        The owner holds `basket:manage` and can SEE Grace's file; the package's subject is
        Ada, so attaching Grace's file to it would put a third party's document into
        somebody else's handover.
        """
        await register_owner(client, mailbox)
        await invite_and_accept(client, mailbox, email="grace@example.com")
        await sign_in(client, mailbox, email="grace@example.com")
        graces = await upload(client, store, filename="grace-notes.txt")

        await sign_in(client, mailbox, email="ada@example.com")
        owner_id = await user_id_of(client, "ada@example.com")
        package = await create_kt(client, subject_user_id=owner_id)

        attached = await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [graces["id"]]},
            headers=csrf(client),
        )

        # 201 with zero attached: the request was well formed and nothing landed. Saying
        # WHICH id failed WHICH check would be a probe of Grace's basket.
        assert attached.status_code == 201, attached.text
        assert attached.json()["attached"] == 0
        assert (await client.get(f"/v1/kt/{package['id']}/attachments")).json()["items"] == []

    async def test_attaching_the_same_file_twice_is_a_no_op_rather_than_an_error(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # A double-submitted form must not fail the whole request.
        package, file_row = await a_shared_package(client, mailbox, store)

        again = await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [file_row["id"]]},
            headers=csrf(client),
        )

        assert again.status_code == 201, again.text
        assert again.json()["attached"] == 0
        assert len((await client.get(f"/v1/kt/{package['id']}/attachments")).json()["items"]) == 1

    async def test_one_bad_id_does_not_discard_the_good_ones(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """The savepoint property, from the outside.

        Postgres aborts a transaction at its first error, so without `begin_nested` the
        duplicate below would discard the attachment that followed it while the request
        still reported success.
        """
        package, first = await a_shared_package(client, mailbox, store)
        second = await upload(client, store, filename="runbook.txt")

        response = await client.post(
            f"/v1/kt/{package['id']}/attachments",
            # first is already attached (a real IntegrityError), then a fresh one.
            json={"file_ids": [first["id"], second["id"]]},
            headers=csrf(client),
        )

        assert response.status_code == 201, response.text
        assert response.json()["attached"] == 1
        names = {
            item["filename"]
            for item in (await client.get(f"/v1/kt/{package['id']}/attachments")).json()["items"]
        }
        assert names == {"notes.txt", "runbook.txt"}

    async def test_the_picker_offers_only_what_the_write_would_accept(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        package, attached = await a_shared_package(client, mailbox, store)
        await upload(client, store, filename="decisions.md", content_type="text/markdown")

        offered = (await client.get(f"/v1/kt/{package['id']}/attachable")).json()["items"]

        names = {item["filename"] for item in offered}
        # The already-attached one is excluded; the spare is offered.
        assert "decisions.md" in names
        assert "notes.txt" not in names
        assert str(attached["id"]) not in {item["file_id"] for item in offered}

    async def test_a_closed_package_refuses_new_attachments_but_allows_withdrawal(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # Withdrawing something is never the act that needs blocking.
        package, file_row = await a_shared_package(client, mailbox, store)
        spare = await upload(client, store, filename="extra.txt")
        await client.post(f"/v1/kt/{package['id']}/revoke", headers=csrf(client))

        refused = await client.post(
            f"/v1/kt/{package['id']}/attachments",
            json={"file_ids": [spare["id"]]},
            headers=csrf(client),
        )
        assert refused.status_code == 409, refused.text

        withdrawn = await client.delete(
            f"/v1/kt/{package['id']}/attachments/{file_row['id']}", headers=csrf(client)
        )
        assert withdrawn.status_code == 204, withdrawn.text


# ------------------------------------------------------------------------ the database


class TestTheDatabaseEnforcesIt:
    async def test_the_attachment_row_carries_its_tenant_and_who_made_it(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        package, file_row = await a_shared_package(client, mailbox, store)

        row = (
            await inspector.execute(
                text(
                    "SELECT org_id, package_id, basket_file_id, attached_by, detached_at "
                    "FROM kt_package_files"
                )
            )
        ).one()

        assert str(row.package_id) == package["id"]
        assert str(row.basket_file_id) == file_row["id"]
        assert row.org_id is not None
        assert row.attached_by is not None
        assert row.detached_at is None

    async def test_detaching_writes_two_columns_rather_than_deleting_the_row(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        # The record that a file WAS shared has to survive its unsharing.
        package, file_row = await a_shared_package(client, mailbox, store)

        await client.delete(
            f"/v1/kt/{package['id']}/attachments/{file_row['id']}", headers=csrf(client)
        )

        row = (
            await inspector.execute(text("SELECT detached_at, detached_by FROM kt_package_files"))
        ).one()
        assert row.detached_at is not None
        assert row.detached_by is not None

    async def test_the_composite_foreign_key_refuses_a_cross_tenant_row(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        """Tenant isolation here is structural, not a predicate somebody remembers.

        The FK is on `(basket_file_id, org_id)`, so a row claiming a file under a
        different `org_id` cannot be written at all — no policy, no application check.
        """
        package, file_row = await a_shared_package(client, mailbox, store)
        stranger = uuid.uuid4()

        # `IntegrityError` is what SQLAlchemy wraps asyncpg's ForeignKeyViolationError
        # in. Naming it rather than bare `Exception` is what makes this a test of the
        # constraint rather than of anything at all going wrong.
        with pytest.raises(IntegrityError):
            await inspector.execute(
                text(
                    "INSERT INTO kt_package_files "
                    "(id, org_id, package_id, basket_file_id, attached_by) "
                    "VALUES (:id, :org, :pkg, :file, :by)"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": stranger,
                    "pkg": package["id"],
                    "file": file_row["id"],
                    "by": await user_id_of(client, "ada@example.com"),
                },
            )
        await inspector.rollback()

    async def test_attaching_is_audited(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        package, file_row = await a_shared_package(client, mailbox, store)
        await client.delete(
            f"/v1/kt/{package['id']}/attachments/{file_row['id']}", headers=csrf(client)
        )

        actions = [
            row.action
            for row in (
                await inspector.execute(
                    text(
                        "SELECT action FROM audit_log "
                        "WHERE action IN ('kt.files_attached', 'kt.file_detached') "
                        "ORDER BY id"
                    )
                )
            ).all()
        ]

        assert actions == ["kt.files_attached", "kt.file_detached"]

    async def test_the_audit_row_records_a_count_and_never_a_filename(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        # The trail is read by more people than a basket is, and §4.9 has no carve-out
        # for a field that is not a log line.
        await a_shared_package(client, mailbox, store)

        meta = (
            await inspector.execute(
                text("SELECT meta_json FROM audit_log WHERE action = 'kt.files_attached'")
            )
        ).scalar_one()

        assert meta == {"attached": 1}


class TestOnePackageCannotReachAnothers:
    async def test_a_file_attached_to_another_package_is_a_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """The join must be bounded by the package, not merely by the attachment.

        Two live packages, two recipients, one file attached to the first. Without
        `a.package_id = :pkg` in the download query every recipient in the organisation
        could turn any attached file id into bytes — and no other test in this file would
        notice, because they all use a single package.
        """
        await register_owner(client, mailbox)
        secret = await upload(client, store, filename="alices-plan.txt")
        owner_id = await user_id_of(client, "ada@example.com")

        first = await create_kt(client, subject_user_id=owner_id)
        second = await create_kt(client, subject_user_id=owner_id)
        attached = await client.post(
            f"/v1/kt/{first['id']}/attachments",
            json={"file_ids": [secret["id"]]},
            headers=csrf(client),
        )
        assert attached.json()["attached"] == 1

        await invite_and_accept(client, mailbox, email="grace@example.com")
        # Grace claims the SECOND package through the POST door, as the console does.
        assert await claim(client, second["kt_code"]) == 200
        assert (await client.get(f"/v1/kt/{second['kt_code']}/files")).json()["items"] == []

        leaked = await client.get(f"/v1/kt/{second['kt_code']}/files/{secret['id']}/download")

        assert leaked.status_code == 404, leaked.text
        assert not any(str(secret["id"]) in key for key, _ in store.signed_downloads)

    async def test_the_listing_is_bounded_by_the_package_too(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        mine = await upload(client, store, filename="mine.txt")
        theirs = await upload(client, store, filename="theirs.txt")
        owner_id = await user_id_of(client, "ada@example.com")
        first = await create_kt(client, subject_user_id=owner_id)
        second = await create_kt(client, subject_user_id=owner_id)
        await client.post(
            f"/v1/kt/{first['id']}/attachments",
            json={"file_ids": [mine["id"]]},
            headers=csrf(client),
        )
        await client.post(
            f"/v1/kt/{second['id']}/attachments",
            json={"file_ids": [theirs["id"]]},
            headers=csrf(client),
        )

        await invite_and_accept(client, mailbox, email="grace@example.com")
        assert await claim(client, first["kt_code"]) == 200

        names = [
            item["filename"]
            for item in (await client.get(f"/v1/kt/{first['kt_code']}/files")).json()["items"]
        ]
        assert names == ["mine.txt"]
