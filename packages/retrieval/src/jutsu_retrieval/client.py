"""The Vertex AI transport, behind an interface (spec §9.3, §4.9, §4.10).

Two things live here and nothing else: turning a request into an HTTP call, and turning
an HTTP result into one of the two error classes in `errors.py`. Batching, retry,
normalisation and token accounting are all in `embeddings.py`, deliberately — a transport
that also retried would make the retry logic untestable without a network.

**`EmbeddingTransport` is a Protocol so the tests do not patch internals.** Almost every
property S6 has to guarantee — order preservation, backoff, error classification, budget
enforcement, truncation rejection — is a property of the *caller*, and each one is
testable against a fake transport with no credential and no network. That is why the seam
is here rather than at the HTTP library.

**Nothing in this module logs a request or a response.** Not the instances, not the
vectors, not an error body. `chunks.text` is masked, but masked is not public (ADR 0005 —
there is no PERSON detector, so masked text still contains names), and a provider error
body echoes the input that caused it. Errors carry a status code and a message this
module wrote; they never carry the payload.

The SDK is deliberately not used. `google-cloud-aiplatform` pulls a very large dependency
tree for one POST, and this needs exactly two things from it: ADC, and a URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Protocol

import httpx

from jutsu_retrieval.config import EmbeddingSettings
from jutsu_retrieval.errors import PermanentEmbeddingError, TransientEmbeddingError

__all__ = [
    "MAX_RETRY_AFTER_S",
    "EmbeddingTransport",
    "VertexTransport",
    "classify_status",
    "parse_retry_after",
]

#: Cloud Platform scope. The runtime service account holds `roles/aiplatform.user` and
#: nothing wider, so the token this scope produces can call the model and very little else.
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

#: 429 is a per-minute quota and clears on its own; 5xx is the provider having a moment.
#: Everything else in 4xx is the request being wrong, and will be wrong identically on
#: every retry.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class EmbeddingTransport(Protocol):
    """One `:predict` call. The only thing `embeddings.py` needs from the network."""

    async def predict(
        self, *, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]: ...


#: The longest `Retry-After` this client will obey. A provider that asks for an hour is
#: telling the operator to come back later, not telling a worker to sleep through it; the
#: job is failed instead so the queue can reschedule it under its own policy.
MAX_RETRY_AFTER_S: Final = 120.0


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """`Retry-After` in seconds, or None when it is absent or unusable.

    RFC 9110 permits two forms and providers use both: delta-seconds (`Retry-After: 30`)
    and an HTTP-date (`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`). Handling only the
    first silently ignores the second, which is the same as having no handling at all on
    a provider that happens to send dates.

    Anything unparseable, negative, or beyond `MAX_RETRY_AFTER_S` returns None so the
    caller falls back to its own bounded backoff rather than trusting a hostile or
    mistaken value.
    """
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        seconds = float(text)
    except ValueError:
        # Not delta-seconds, so try the HTTP-date form. `parsedate_to_datetime` raises
        # on anything it cannot read (it stopped returning None in 3.10), and a header
        # we cannot read is one we decline to obey.
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        reference = now if now is not None else datetime.now(UTC).timestamp()
        seconds = parsed.timestamp() - reference

    if seconds < 0 or seconds > MAX_RETRY_AFTER_S:
        return None
    return seconds


def classify_status(
    status: int, *, detail: str = "", retry_after: float | None = None
) -> Exception | None:
    """Map an HTTP status onto the retry decision. `None` means success.

    `detail` is a message this codebase wrote — a status name at most. The provider's
    body never reaches here, because it quotes the input that failed.
    """
    if 200 <= status < 300:
        return None
    if status in _RETRYABLE_STATUSES:
        return TransientEmbeddingError(
            f"embedding request failed, retryable{detail}",
            status=status,
            retry_after=retry_after,
        )
    return PermanentEmbeddingError(
        f"embedding request rejected, not retryable{detail}", status=status
    )


@dataclass(frozen=True, slots=True)
class _HttpxAuthResponse:
    """The three attributes `google.auth` reads off a transport response."""

    status: int
    headers: Mapping[str, str]
    data: bytes


class _HttpxAuthRequest:
    """A `google.auth` transport over httpx.

    google-auth ships `google.auth.transport.requests`, which needs the `requests`
    library — an entire synchronous HTTP stack, brought in for one token refresh, when
    this package already has httpx. The transport interface is three attributes and one
    call, so implementing it is smaller than the dependency would be.

    Synchronous on purpose: `Credentials.refresh` is synchronous, and it is called from a
    worker thread (see `_token`) so the event loop is never blocked.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_kwargs: Any,
    ) -> _HttpxAuthResponse:
        with httpx.Client(timeout=timeout or 30.0) as client:
            response = client.request(method, url, content=body, headers=dict(headers or {}))
        return _HttpxAuthResponse(
            status=response.status_code, headers=response.headers, data=response.content
        )


class VertexTransport:
    """`gemini-embedding-001` over REST, authenticated with Application Default Credentials.

    ADC rather than a key file: Cloud Run supplies the attached service account and a key
    on disk is a credential that can be copied. `GOOGLE_APPLICATION_CREDENTIALS` stays
    empty in every deployed environment.
    """

    __slots__ = ("_client", "_credentials", "_lock", "_settings")

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        client: httpx.AsyncClient | None = None,
        credentials: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout_s)
        self._credentials = credentials
        # Refreshing is not reentrant and several batches may notice an expired token at
        # once; without this they would all refresh, and google-auth would race on the
        # shared credential object.
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _token(self) -> str:
        """A bearer token, refreshed when stale.

        `google.auth` is synchronous and its refresh performs network IO, so it goes to a
        worker thread rather than blocking the event loop while a corpus job is running.
        """
        async with self._lock:
            if self._credentials is None:
                import google.auth

                credentials, _project = await asyncio.to_thread(google.auth.default, scopes=_SCOPES)
                self._credentials = credentials

            if not getattr(self._credentials, "valid", False):
                await asyncio.to_thread(self._credentials.refresh, _HttpxAuthRequest())

            token = getattr(self._credentials, "token", None)
            if not token:
                raise PermanentEmbeddingError(
                    "Application Default Credentials produced no access token. Run "
                    "`gcloud auth application-default login` locally; in a deployed "
                    "environment check the attached service account."
                )
            return str(token)

    async def predict(
        self, *, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        token = await self._token()
        try:
            response = await self._client.post(
                self._settings.endpoint,
                json={"instances": instances, "parameters": parameters},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=self._settings.request_timeout_s,
            )
        except httpx.TimeoutException as error:
            raise TransientEmbeddingError("embedding request timed out") from error
        except httpx.HTTPError as error:
            # A connection-level failure is the network, not the request. Retryable, and
            # the exception text is not included — httpx puts the URL in it, which is
            # harmless, but the habit of forwarding library messages is not.
            raise TransientEmbeddingError("embedding request failed to complete") from error

        failure = classify_status(
            response.status_code,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
        if failure is not None:
            raise failure

        payload: dict[str, Any] = response.json()
        return payload
