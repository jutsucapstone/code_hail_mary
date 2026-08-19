"""Organisation registration — the entry point for a pilot.

Public, because there is nobody to authenticate yet: this route is what brings the first
account into existence.

The response is identical whether or not the domain was already registered. That is not
politeness, it is the difference between a sign-up form and a customer-enumeration
endpoint — without it, anyone could probe domains to learn which companies use JUTSU.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, EmailStr, Field

from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import Db, get_email_sender
from jutsu_api.email import EmailSender
from jutsu_api.registration import RegistrationRequest, register_organisation
from jutsu_api.security import GuardedAPIRoute, public

router = APIRouter(prefix="/v1/orgs", tags=["orgs"], route_class=GuardedAPIRoute)

SettingsDep = Annotated[Settings, Depends(get_settings)]
SenderDep = Annotated[EmailSender, Depends(get_email_sender)]


class RegisterPayload(BaseModel):
    # `extra="forbid"` is a security control, not tidiness: without it a client could
    # post `org_id`, `role` or `status` and a future handler that widened its model would
    # silently accept them. Mass assignment is the classic way a registration form
    # becomes a privilege-escalation endpoint.
    model_config = {"extra": "forbid"}

    full_name: str = Field(min_length=1, max_length=255)
    work_email: EmailStr
    company_name: str = Field(min_length=1, max_length=255)
    company_domain: str = Field(min_length=3, max_length=255)
    job_title: str = Field(min_length=1, max_length=128)
    org_size: str = Field(min_length=1, max_length=16)


class RegistrationAccepted(BaseModel):
    """Deliberately says nothing about what happened.

    No organisation id, no JUTSU ID, no "already exists". The next step is in the
    recipient's inbox, which is the only channel that proves they control the address.
    """

    status: str = "check_your_email"


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
@public("Registration creates the first account; requiring a session would be circular.")
async def register(
    payload: RegisterPayload,
    session: Db,
    settings: SettingsDep,
    sender: SenderDep,
) -> RegistrationAccepted:
    await register_organisation(
        session,
        RegistrationRequest(
            full_name=payload.full_name,
            work_email=str(payload.work_email),
            company_name=payload.company_name,
            company_domain=payload.company_domain,
            job_title=payload.job_title,
            org_size=payload.org_size,
        ),
        settings=settings,
        sender=sender,
    )
    # The outcome is deliberately discarded rather than returned: whether an organisation
    # was created is exactly the fact this endpoint must not disclose.
    return RegistrationAccepted()
