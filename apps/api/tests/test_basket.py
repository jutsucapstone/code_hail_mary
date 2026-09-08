"""The Knowledge Basket over the wire, against real Postgres and a fake bucket.

The bucket is fake and the database is not, which is the right way round: what needs
proving here is who may reach a row — row-level security, the ownership boundary, the
audit row — and none of that involves Cloud Storage. What the store does with bytes is
proven in `packages/core/tests/test_storage.py` without a network either.

Three properties carry most of the weight:

  * an employee sees their own files and nobody else's, and an administrator holding
    `basket:manage` sees the organisation's;
  * nothing the client says about its own upload is trusted — size, checksum and type
    all come from the store after the fact;
  * a file whose text cannot be reached is `stored`, a terminal success, rather than
    left processing for ever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender, get_object_store
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

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
TEXT_BYTES = b"The handover notes, in plain text."
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EXE_BYTES = b"MZ\x90\x00\x03" + b"\x00" * 64


class FakeStore:
    """A bucket in a dict.

    Records what was signed so the tests can assert the key never carried user input,
    and lets a test place bytes under a key to simulate what the browser PUT.
    """

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


async def upload(
    client: AsyncClient,
    store: FakeStore,
    *,
    filename: str = "notes.txt",
    content_type: str = "text/plain",
    body: bytes = TEXT_BYTES,
    put: bool = True,
) -> dict[str, Any]:
    """The whole browser dance: ask for a URL, PUT the bytes, tell the API."""
    started = await client.post(
        "/v1/basket/files",
        json={
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(body),
        },
        headers=csrf(client),
    )
    assert started.status_code == 201, started.text
    ticket = started.json()

    if put:
        key = ticket["url"].removeprefix("https://storage.example/")
        store.objects[key] = body

    done = await client.post(f"/v1/basket/files/{ticket['file_id']}/complete", headers=csrf(client))
    assert done.status_code == 200, done.text
    result: dict[str, Any] = done.json()
    return result


# --------------------------------------------------------------- the upload lifecycle


class TestUploading:
    async def test_a_text_file_becomes_searchable_and_queues_its_ingestion(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)

        row = await upload(client, store)

        assert row["state"] == "uploaded"
        assert row["filename"] == "notes.txt"
        # The pipeline is durable work on the existing queue, not something the employee
        # waits for inside the request.
        queued = (
            await inspector.execute(
                text("SELECT kind, state, idempotency_key FROM jobs WHERE kind = 'ingest.document'")
            )
        ).all()
        assert len(queued) == 1
        assert queued[0].state == "pending"
        assert str(row["id"]) in queued[0].idempotency_key

    async def test_the_object_key_never_carries_the_filename(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # Path traversal is unrepresentable rather than filtered, and this is where that
        # claim is checked against a real request.
        await register_owner(client, mailbox)

        await upload(client, store, filename="../../etc/passwd")

        key, _, _ = store.signed_uploads[0]
        assert ".." not in key
        assert "passwd" not in key
        assert key.startswith("org/")

    async def test_the_signed_url_is_pinned_to_the_declared_size(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)

        await upload(client, store, body=b"x" * 128)

        _, _, max_bytes = store.signed_uploads[0]
        assert max_bytes == 128

    async def test_an_image_is_stored_and_says_why_it_is_not_searchable(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        """The honesty requirement: never left processing for a format nothing can read."""
        await register_owner(client, mailbox)

        row = await upload(
            client, store, filename="screenshot.png", content_type="image/png", body=PNG_BYTES
        )

        assert row["state"] == "stored"
        assert row["searchable"] is False
        assert "not searched" in (row["detail"] or "")
        # And no ingestion job was queued, because there is nothing to extract.
        jobs = (
            await inspector.execute(
                text("SELECT count(*) FROM jobs WHERE kind = 'ingest.document'")
            )
        ).scalar_one()
        assert jobs == 0

    async def test_an_executable_announced_as_an_image_is_rejected(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # The oldest upload attack there is. The declared type pinned the signed URL;
        # the bytes are what decides.
        await register_owner(client, mailbox)

        row = await upload(
            client, store, filename="cat.png", content_type="image/png", body=EXE_BYTES
        )

        assert row["state"] == "rejected"
        assert "do not match" in (row["detail"] or "")

    async def test_an_upload_that_never_arrived_is_refused_not_completed(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        started = await client.post(
            "/v1/basket/files",
            json={"filename": "ghost.txt", "content_type": "text/plain", "size_bytes": 10},
            headers=csrf(client),
        )
        file_id = started.json()["file_id"]

        # No PUT happened.
        done = await client.post(f"/v1/basket/files/{file_id}/complete", headers=csrf(client))

        assert done.status_code == 422, done.text

    async def test_completing_twice_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # The browser retrying must not re-verify or re-enqueue.
        await register_owner(client, mailbox)
        row = await upload(client, store)

        again = await client.post(f"/v1/basket/files/{row['id']}/complete", headers=csrf(client))

        assert again.status_code == 409, again.text

    async def test_a_type_nothing_accepts_is_refused_before_any_bytes_move(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        refused = await client.post(
            "/v1/basket/files",
            json={
                "filename": "installer.exe",
                "content_type": "application/x-msdownload",
                "size_bytes": 1024,
            },
            headers=csrf(client),
        )

        assert refused.status_code == 422, refused.text

    async def test_an_oversized_file_is_refused_by_the_request_model(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)

        refused = await client.post(
            "/v1/basket/files",
            json={
                "filename": "huge.txt",
                "content_type": "text/plain",
                "size_bytes": 999_999_999_999,
            },
            headers=csrf(client),
        )

        assert refused.status_code == 422, refused.text


# --------------------------------------------------------------------- who sees what


class TestOneEmployeeCannotSeeAnother:
    async def test_a_colleague_cannot_list_or_reach_your_files(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        """The boundary inside an organisation, which RLS does not provide.

        Both people are in the same tenant, so row-level security passes them both. What
        separates them is `_visible_to`, applied inside the query.
        """
        await register_owner(client, mailbox)
        mine = await upload(client, store, filename="my-notes.txt")

        # Invite a plain Member and become them. Accepting signs the client in.
        invited = await client.post(
            "/v1/employees/invitations",
            json={"email": "colleague@example.com", "role": "member"},
            headers=csrf(client),
        )
        assert invited.status_code == 202, invited.text
        accepted = await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )
        assert accepted.status_code == 200, accepted.text

        listed = await client.get("/v1/basket/files")
        fetched = await client.get(f"/v1/basket/files/{mine['id']}/download")
        renamed = await client.patch(
            f"/v1/basket/files/{mine['id']}",
            json={"filename": "stolen.txt"},
            headers=csrf(client),
        )
        removed = await client.delete(f"/v1/basket/files/{mine['id']}", headers=csrf(client))

        assert listed.status_code == 200
        assert listed.json()["items"] == [], "a colleague saw somebody else's basket"
        # Not 403: a file you may not see does not exist as far as you are concerned.
        assert fetched.status_code == 404, fetched.text
        assert renamed.status_code == 404, renamed.text
        assert removed.status_code == 404, removed.text

    async def test_a_member_can_still_use_their_own_basket(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # The permission is universal on purpose — an admin-gated basket would be
        # unusable by the people it exists for.
        await register_owner(client, mailbox)
        await client.post(
            "/v1/employees/invitations",
            json={"email": "colleague@example.com", "role": "member"},
            headers=csrf(client),
        )
        await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )

        theirs = await upload(client, store, filename="their-notes.txt")

        assert theirs["state"] == "uploaded"
        listed = await client.get("/v1/basket/files")
        assert [item["filename"] for item in listed.json()["items"]] == ["their-notes.txt"]


class TestAdministratorsSeeTheOrganisation:
    async def test_an_owner_holding_basket_manage_sees_everybodys_files(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        db_session: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        await upload(client, store, filename="owner-notes.txt")

        # A colleague uploads their own, then the owner signs back in.
        await client.post(
            "/v1/employees/invitations",
            json={"email": "colleague@example.com", "role": "member"},
            headers=csrf(client),
        )
        await client.post(
            "/v1/invitations/accept",
            json={"token": mailbox.last.secrets["token"], "full_name": "Charles Babbage"},
        )
        await upload(client, store, filename="member-notes.txt")

        # Back to the owner: sign in again with the magic-link flow.
        await client.post("/v1/auth/request", json={"email": "ada@example.com"})
        delivered = mailbox.last.secrets
        signed_in = await client.post(
            "/v1/auth/verify",
            json={"token": delivered["token"], "code": delivered["code"]},
        )
        assert signed_in.status_code == 200, signed_in.text

        listed = await client.get("/v1/basket/files")

        names = sorted(item["filename"] for item in listed.json()["items"])
        assert names == ["member-notes.txt", "owner-notes.txt"]


# ------------------------------------------------------------------ the rest of the API


class TestDownloadRenameDeleteRetry:
    async def test_a_download_url_is_minted_only_after_the_row_came_back(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        row = await upload(client, store)

        response = await client.get(f"/v1/basket/files/{row['id']}/download")

        assert response.status_code == 200
        assert response.json()["url"].startswith("https://storage.example/org/")
        # The download filename is set server-side from the column, so a client cannot
        # choose what a saved file is called.
        _, filename = store.signed_downloads[0]
        assert filename == "notes.txt"

    async def test_a_file_still_uploading_cannot_be_downloaded(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        await register_owner(client, mailbox)
        started = await client.post(
            "/v1/basket/files",
            json={"filename": "pending.txt", "content_type": "text/plain", "size_bytes": 5},
            headers=csrf(client),
        )

        response = await client.get(f"/v1/basket/files/{started.json()['file_id']}/download")

        assert response.status_code == 409, response.text

    async def test_renaming_keeps_the_sort_key_in_step_with_the_display_name(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        row = await upload(client, store)

        renamed = await client.patch(
            f"/v1/basket/files/{row['id']}",
            json={"filename": "Q3 Handover.txt"},
            headers=csrf(client),
        )

        assert renamed.status_code == 200
        assert renamed.json()["filename"] == "Q3 Handover.txt"
        stored = (
            await inspector.execute(
                text("SELECT normalised_filename FROM basket_files WHERE id = :id"),
                {"id": UUID(row["id"])},
            )
        ).scalar_one()
        # A listing that sorts on one string and matches on another is how a file
        # appears to be missing.
        assert stored == "q3 handover.txt"

    async def test_deleting_hides_it_at_once_and_leaves_an_audit_row(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        row = await upload(client, store)

        removed = await client.delete(f"/v1/basket/files/{row['id']}", headers=csrf(client))

        assert removed.status_code == 204
        listed = await client.get("/v1/basket/files")
        assert listed.json()["items"] == []
        # Soft: the row survives as the record that the file existed.
        surviving = (
            await inspector.execute(
                text("SELECT deleted_at IS NOT NULL AS gone FROM basket_files WHERE id = :id"),
                {"id": UUID(row["id"])},
            )
        ).scalar_one()
        assert surviving is True
        audited = (
            await inspector.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'basket.file_deleted'")
            )
        ).scalar_one()
        assert audited == 1

    async def test_a_deleted_file_cannot_be_downloaded(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        row = await upload(client, store)
        await client.delete(f"/v1/basket/files/{row['id']}", headers=csrf(client))

        response = await client.get(f"/v1/basket/files/{row['id']}/download")

        assert response.status_code == 404

    async def test_only_a_failed_file_can_be_retried(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # Offering retry where it would fail identically teaches people the button does
        # nothing.
        await register_owner(client, mailbox)
        stored_only = await upload(
            client, store, filename="clip.png", content_type="image/png", body=PNG_BYTES
        )

        response = await client.post(
            f"/v1/basket/files/{stored_only['id']}/retry", headers=csrf(client)
        )

        assert response.status_code == 409, response.text
        assert stored_only["retryable"] is False


class TestSearchAndFilter:
    async def test_search_matches_the_normalised_name(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        await upload(client, store, filename="Q3 Handover Notes.txt")
        await upload(
            client, store, filename="expenses.csv", content_type="text/csv", body=b"a,b\n1,2\n"
        )

        found = await client.get("/v1/basket/files", params={"q": "HANDOVER"})

        assert [item["filename"] for item in found.json()["items"]] == ["Q3 Handover Notes.txt"]

    async def test_filtering_by_state(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        await register_owner(client, mailbox)
        await upload(client, store)
        await upload(client, store, filename="a.png", content_type="image/png", body=PNG_BYTES)

        stored = await client.get("/v1/basket/files", params={"state": "stored"})

        assert [item["filename"] for item in stored.json()["items"]] == ["a.png"]


class TestAuthorization:
    async def test_every_route_refuses_without_a_session(self, client: AsyncClient) -> None:
        target = "/v1/basket/files/11111111-1111-4111-8111-111111111111"

        assert (await client.get("/v1/basket/files")).status_code == 401
        assert (
            await client.post(
                "/v1/basket/files",
                json={"filename": "x.txt", "content_type": "text/plain", "size_bytes": 1},
            )
        ).status_code == 401
        assert (await client.get(f"{target}/download")).status_code == 401
        assert (
            await client.post(f"{target}/complete", headers={"Content-Length": "0"})
        ).status_code == 401
        assert (await client.delete(target)).status_code == 401

    async def test_the_routes_answer_503_when_no_bucket_is_configured(
        self, db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
    ) -> None:
        """A deployment without storage runs normally and says so.

        Raising at startup would take the whole API down over a feature most requests
        never touch.
        """
        app = create_app()

        async def _db() -> AsyncIterator[AsyncSession]:
            yield db_session
            await db_session.commit()

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_email_sender] = lambda: mailbox
        app.dependency_overrides[get_object_store] = lambda: None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as http:
            await register_owner(http, mailbox)
            response = await http.post(
                "/v1/basket/files",
                json={"filename": "x.txt", "content_type": "text/plain", "size_bytes": 4},
                headers=csrf(http),
            )

        assert response.status_code == 503, response.text
        assert "not configured" in response.json()["error"]["message"]


class TestHostileFilenames:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\cmd.exe",
            "report‮gnp.txt",
            "with\x00nul.txt",
            # 500 characters: inside the request model's bound, so it must be accepted
            # and shortened rather than refused. The over-long case is its own test
            # below — a 422 and a sanitised upload are different behaviours, and one
            # parametrised assertion cannot honestly cover both.
            "a" * 500,
        ],
    )
    async def test_they_are_stored_safely_and_never_reach_a_key(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        hostile: str,
    ) -> None:
        await register_owner(client, mailbox)

        row = await upload(client, store, filename=hostile)

        key, _, _ = store.signed_uploads[0]
        assert key.startswith("org/")
        assert ".." not in key
        assert "\x00" not in key
        # The row exists and is usable; the name was neutralised, not the upload.
        assert row["state"] == "uploaded"
        # And what comes back is storable, renderable text.
        assert "\x00" not in row["filename"]
        assert "‮" not in row["filename"]
        assert len(row["filename"]) <= 255

    async def test_a_filename_past_the_request_bound_is_refused_at_the_boundary(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        # Bounded input is the point: the model refuses before a row or a signed URL
        # exists. 512 is that bound; `sanitise_original` shortens to 255 below it.
        await register_owner(client, mailbox)

        refused = await client.post(
            "/v1/basket/files",
            json={"filename": "a" * 600, "content_type": "text/plain", "size_bytes": 10},
            headers=csrf(client),
        )

        assert refused.status_code == 422, refused.text


class TestConcurrentAndDuplicateUploads:
    async def test_two_files_with_the_same_name_are_two_files(
        self, client: AsyncClient, mailbox: RecordingEmailSender, store: FakeStore
    ) -> None:
        # A basket is not a filesystem; the same name twice is normal and must not
        # overwrite. Distinct server-generated keys are what guarantee it.
        await register_owner(client, mailbox)

        first = await upload(client, store, filename="notes.txt", body=b"first version")
        second = await upload(client, store, filename="notes.txt", body=b"second version")

        assert first["id"] != second["id"]
        keys = {key for key, _, _ in store.signed_uploads}
        assert len(keys) == 2
        listed = await client.get("/v1/basket/files")
        assert len(listed.json()["items"]) == 2

    async def test_re_completing_does_not_queue_a_second_job(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        store: FakeStore,
        inspector: AsyncSession,
    ) -> None:
        await register_owner(client, mailbox)
        row = await upload(client, store)
        await client.post(f"/v1/basket/files/{row['id']}/complete", headers=csrf(client))

        jobs = (
            await inspector.execute(
                text("SELECT count(*) FROM jobs WHERE kind = 'ingest.document'")
            )
        ).scalar_one()
        assert jobs == 1
