"""Sign-in, verification and sign-out.

All three are `@public`: an endpoint that issues sessions cannot require one. That is
also why they are the most carefully bounded routes in the service — everything else can
lean on a resolved principal, and these cannot.

**Every response on this path is deliberately uninformative.** `POST /request` returns the
same 202 whether or not the address has an account, and `POST /verify` returns the same
error for a wrong code, an unknown token, an expired challenge and an exhausted attempt
budget. Anything more helpful is an oracle.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from jutsu_core.errors import Unauthenticated
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from jutsu_api.auth_service import (
    ChallengePurpose,
    issue_challenge,
    open_session,
    revoke_session,
    scoped_role,
    spend_sign_in_budget,
    verify_challenge,
)
from jutsu_api.config import (
    OTP_DIGITS,
    SESSION_ABSOLUTE_TTL_SECONDS,
    Settings,
    get_settings,
)
from jutsu_api.deps import Db, get_email_sender
from jutsu_api.email import EmailSender
from jutsu_api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    GuardedAPIRoute,
    destination_for,
    public,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"], route_class=GuardedAPIRoute)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[EmailSender, Depends(get_email_sender)]


class ChallengeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr
    #: Optional cross-check, never an authorisation input. A JUTSU id is 18 characters;
    #: the headroom is for the hand-typed forms the Crockford normaliser repairs. When
    #: present, the code is delivered only if the id and the address resolve to the same
    #: membership — the response is the identical 202 either way.
    jutsu_id: str | None = Field(default=None, max_length=24)


class ChallengeAccepted(BaseModel):
    """Identical for every input. Carries no signal about whether the account exists."""

    status: str = "sent"


class VerifyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    token: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=OTP_DIGITS, max_length=OTP_DIGITS)


class VerifyResult(BaseModel):
    #: Chosen by the server, never by the client. A `next` parameter accepted here would
    #: be an open redirect with a session attached.
    destination: str


def set_session_cookies(
    response: Response, *, token: str, csrf_token: str, settings: Settings
) -> None:
    """The session handle is httpOnly; its CSRF partner deliberately is not.

    The CSRF value has to be readable by our own page so it can be echoed in a header —
    that is the whole double-submit mechanism. It is not a credential on its own: without
    the httpOnly session cookie it authorises nothing.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookies_secure,
        samesite="lax",
        path="/",
        max_age=SESSION_ABSOLUTE_TTL_SECONDS,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        httponly=False,
        secure=settings.cookies_secure,
        samesite="lax",
        path="/",
        max_age=SESSION_ABSOLUTE_TTL_SECONDS,
    )


@router.post("/request", status_code=status.HTTP_202_ACCEPTED)
@public("Sign-in cannot require a session; issuing one is what it does.")
async def request_challenge(
    payload: ChallengeRequest,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> ChallengeAccepted:
    # Before any mail or any auth-schema write. Each request delivers a message to an
    # address the caller names and writes an identity row and a challenge — an open
    # relay without a ceiling, exactly like staging (§20's precedent).
    await spend_sign_in_budget(session, address=str(payload.email), settings=settings)
    await issue_challenge(
        session,
        address=str(payload.email),
        purpose=ChallengePurpose.SIGN_IN,
        settings=settings,
        sender=sender,
        jutsu_id=payload.jutsu_id,
    )
    return ChallengeAccepted()


@router.post("/verify")
@public("Redeeming a challenge is how a session comes into existence.")
async def verify(
    payload: VerifyRequest,
    response: Response,
    session: Db,
    settings: SettingsDep,
) -> VerifyResult:
    # `SIGN_IN` only. A registration code redeemed here would find no membership and be
    # refused anyway, but relying on that is relying on a side effect — once the two
    # flows share one challenge namespace, the purpose has to be checked, not inferred.
    redeemed = await verify_challenge(
        session,
        token=payload.token,
        code=payload.code,
        expected_purpose=ChallengePurpose.SIGN_IN,
    )
    identity_id = redeemed.identity_id

    memberships = (
        await session.execute(
            text("SELECT org_id, user_id FROM auth.resolve_memberships(:i)"),
            {"i": identity_id},
        )
    ).all()
    if not memberships:
        # The code was right but the identity belongs to no organisation — an address
        # that requested a challenge without ever registering. Same refusal as a bad
        # code, so the two cannot be told apart.
        raise Unauthenticated("That code is not valid.")

    # P1 opens the first membership. Choosing between several is a later slice; doing it
    # from a client-supplied org id would be an authorisation input from the browser,
    # which is exactly what this architecture refuses.
    org_id, user_id = memberships[0]

    credentials = await open_session(
        session, identity_id=identity_id, user_id=user_id, org_id=org_id
    )
    set_session_cookies(
        response,
        token=credentials.token,
        csrf_token=credentials.csrf_token,
        settings=settings,
    )

    # Read after the membership is known, and inside the org scope — `scoped_role` sets
    # the GUC first for that reason. Sending everyone to /admin was the old behaviour and
    # it was wrong for exactly the people this flow exists to serve: an invited Member
    # signed in successfully and arrived at a dashboard that answered 403 to every
    # request it made, which reads like a broken product rather than a wrong redirect.
    role = await scoped_role(session, org_id=org_id, user_id=user_id)
    return VerifyResult(destination=destination_for(role))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@public("Signing out must work even from an already-invalid session.")
async def logout(
    request: Request, response: Response, session: Db, settings: SettingsDep
) -> Response:
    """Revokes server-side and clears the cookies, whatever state the session was in.

    Deliberately not behind `get_principal`. A logout that refuses because the session
    had already expired would leave a stale cookie in the browser and give the user no
    way to clear it — the one moment a strict check makes things less safe, not more.

    Revocation is server-side as well as cookie-clearing: deleting the cookie alone
    leaves the handle valid, so anyone who captured it keeps a working session.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(session, token=token)

    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookies_secure)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookies_secure)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
