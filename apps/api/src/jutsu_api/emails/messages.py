"""The six authentication emails, and the line between them that matters.

    organisation_verification   onboarding, before the tenant exists   OTP,  no token
    organisation_welcome        onboarding, after the tenant exists    no OTP,  token
    employee_invitation         a person joining an existing tenant    invite,  no token
    employee_welcome            that person, once they have joined     no OTP,  own id
    sign_in_code                everybody, every time after that       OTP,  no token
    no_account                  an address with nothing behind it      nothing

**The organisation identifier appears in exactly one message.** `organisation_welcome` is
the only builder that takes an `org_id`, and it is reachable only from the moment the
organisation is created — which is itself reachable only by someone who has proved a
mailbox and redeemed a registration challenge. Every other builder is typed so that it
cannot carry one: there is no parameter to pass it through. That is deliberate, and it is
the reason this is six narrow functions rather than one with a dictionary of optional
fields — an optional field is a field somebody eventually populates from the wrong flow,
and a returning employee's sign-in mail is the last place a tenant identifier should
appear.

**A person's own JUTSU ID is not an organisation token.** `employee_welcome` carries one
because the console sign-in form asks for it by name, and because acceptance currently
shows it on one screen that is gone the moment the tab closes. It identifies; it
authenticates nothing on its own. The organisation id, by contrast, is tenant-scoped and
stays with the administrator who created the tenant.

Codes and tokens are never passed into these functions. They emit `[[code]]` and
`[[token]]` and the transport substitutes the real values at render time, so a template
can be rendered, diffed and asserted against without a live credential ever existing in
the same object.
"""

from __future__ import annotations

from jutsu_api.email import EmailMessage, secret_slot
from jutsu_api.emails import components as ui
from jutsu_api.emails.assets import brand_mark
from jutsu_api.emails.layout import document, text_document

__all__ = [
    "CODE_SLOT",
    "TOKEN_SLOT",
    "employee_invitation",
    "employee_welcome",
    "no_account",
    "organisation_verification",
    "organisation_welcome",
    "sign_in_code",
]

#: What the templates emit where a one-time value belongs, keyed to the names
#: `issue_challenge` and `invite_employee` put on `EmailMessage.secrets`.
#:
#: Derived from `secret_slot` rather than spelled out, so a template and the
#: substitution that fills it cannot disagree by a character — that failure renders
#: `[[code]]` where the code should be, and the first person to see it is a customer.
CODE_SLOT = secret_slot("code")
TOKEN_SLOT = secret_slot("token")

_SIGNATURE = "JUTSU — Corporate Memory Graph"
_NO_REPLY = "This is an automated message from an unmonitored address. Please do not reply."


def _verify_url(app_url: str, *, flow: str | None = None) -> str:
    """A plain URL. Escaping belongs to whoever puts it in markup, and happens once.

    This returned `&amp;` for a while, on the reasoning that the value lands in an `href`.
    It does — and `action_button` escapes what it is given, so the ampersand was escaped
    twice and shipped as `&amp;amp;`. A browser decodes that to a literal `&amp;` and
    parses the second parameter as `amp;flow`, which drops `flow` entirely.

    That was not cosmetic. `/pilot/verify` reads `flow=register` to decide whether to call
    `completeRegistration` or `verify`; without it a registration code is submitted to the
    sign-in endpoint, fails the `expected_purpose` check, and costs the registrant one of
    their five attempts — on the first link the product ever sends them.
    """
    query = f"token={TOKEN_SLOT}" + (f"&flow={flow}" if flow else "")
    return f"{app_url}/pilot/verify?{query}"


def _footer_lines(*extra: str) -> list[str]:
    return [*extra, _NO_REPLY, _SIGNATURE]


def _compose(*, to: str, subject: str, text: str, html: str) -> EmailMessage:
    """The one constructor every builder returns through.

    The mark is attached here rather than at each call site for the reason every other
    shared section exists: a builder that forgot it would send a message with a broken
    image, and nothing in the type system or the tests would notice until somebody
    opened one.
    """
    return EmailMessage(
        to=to,
        subject=subject,
        body=text,
        html=html,
        inline_images=(brand_mark(),),
    )


def organisation_verification(
    *, to: str, company_name: str, company_domain: str, app_url: str, minutes: int
) -> EmailMessage:
    """Onboarding, step one: prove the mailbox before anything is created.

    Carries no organisation identifier because there is no organisation — nothing durable
    exists until the code comes back, which is the control that stops a domain being
    claimed by anyone who can type it. The company name and domain shown here are the
    values the registrant typed minutes ago, echoed so a typo in either is caught before
    a tenant is built around it rather than afterwards.
    """
    expiry = f"Expires in {minutes} minutes. One use only."
    html = document(
        preheader=f"Your verification code for {company_name} on JUTSU.",
        sections=[
            ui.header("Organisation setup"),
            ui.graph_strip(),
            ui.hero(
                f"Let's get {company_name} onto JUTSU",
                "Enter the code below to verify this address and create your "
                "organisation. Nothing is created until you do.",
            ),
            ui.code_panel(label="Verification code", slot=CODE_SLOT, caption=expiry),
            ui.action_button(
                label="Open the verification page",
                href=_verify_url(app_url, flow="register"),
            ),
            ui.detail_card(
                title="Organisation being created",
                rows=[("Company", company_name), ("Domain", company_domain)],
            ),
            ui.notice(
                title="Before you continue",
                points=[
                    "Check the company name and domain above. They are what your "
                    "organisation will be created with.",
                    "JUTSU will never ask you for this code by phone, chat or email.",
                    "If you did not start this, ignore this message — no organisation "
                    "is created without the code.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to} because it was used to register.")),
        ],
    )
    text = text_document(
        heading=f"Verify {company_name} on JUTSU",
        blocks=[
            "Enter this code to verify this address and create your organisation:",
            f"    {CODE_SLOT}",
            expiry,
            f"Organisation: {company_name}\nDomain:       {company_domain}",
            f"Or open: {_verify_url(app_url, flow='register')}",
            "JUTSU will never ask you for this code by phone, chat or email. If you "
            "did not start this, ignore this message — no organisation is created "
            "without the code.",
        ],
        footer_lines=_footer_lines(f"Sent to {to} because it was used to register."),
    )
    return _compose(to=to, subject=f"Verify {company_name} on JUTSU", text=text, html=html)


def organisation_welcome(
    *,
    to: str,
    company_name: str,
    company_domain: str,
    org_id: str,
    jutsu_id: str,
    role: str,
    app_url: str,
) -> EmailMessage:
    """Onboarding, step two: the tenant exists, and here is what identifies it.

    **The only message that carries an organisation identifier.** It is sent to the
    address that has just proved a mailbox and redeemed the registration challenge, at
    the moment the organisation comes into existence and to nobody else, ever again. A
    returning administrator signing in gets `sign_in_code`, which has no parameter for
    one.

    No code, and that is not an omission. Whoever reads this was signed in by the
    redemption that created the organisation — issuing a second credential they did not
    ask for would put a live one-time code in an inbox for no reason, which is a
    liability rather than a convenience.
    """
    html = document(
        preheader=f"{company_name} is live on JUTSU. Keep your organisation ID safe.",
        sections=[
            ui.header("Welcome aboard"),
            ui.graph_strip(),
            ui.hero(
                f"{company_name} is live on JUTSU",
                "Your organisation has been created and you are its owner. The "
                "identifiers below are what your organisation and your account are "
                "known by.",
            ),
            ui.detail_card(
                title="Your organisation",
                rows=[
                    ("Organisation", company_name),
                    ("Domain", company_domain),
                    ("Organisation ID", org_id),
                    ("Your JUTSU ID", jutsu_id),
                    ("Your role", role),
                ],
            ),
            ui.action_button(label="Open your console", href=f"{app_url}/admin"),
            ui.paragraph(
                "Your JUTSU ID is what the console asks for when you sign in again, "
                "alongside this email address. There is no password to set."
            ),
            ui.notice(
                title="Keep these safe",
                points=[
                    "The organisation ID identifies your whole tenant. Share it only "
                    "with people who administer JUTSU for your organisation.",
                    "Your colleagues do not need it — invite them from the console and "
                    "they each receive their own JUTSU ID.",
                    "Signing in always sends a fresh one-time code to this address. "
                    "Neither identifier is a password.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to}, the owner of {company_domain}.")),
        ],
    )
    text = text_document(
        heading=f"{company_name} is live on JUTSU",
        blocks=[
            "Your organisation has been created and you are its owner.",
            (
                f"Organisation:    {company_name}\n"
                f"Domain:          {company_domain}\n"
                f"Organisation ID: {org_id}\n"
                f"Your JUTSU ID:   {jutsu_id}\n"
                f"Your role:       {role}"
            ),
            f"Console: {app_url}/admin",
            "Your JUTSU ID is what the console asks for when you sign in again, "
            "alongside this email address. There is no password to set.",
            "Keep the organisation ID to administrators only. Your colleagues do not "
            "need it — invite them from the console and they each receive their own "
            "JUTSU ID.",
        ],
        footer_lines=_footer_lines(f"Sent to {to}, the owner of {company_domain}."),
    )
    return _compose(to=to, subject=f"{company_name} is live on JUTSU", text=text, html=html)


def employee_invitation(*, to: str, organisation: str, app_url: str, hours: int) -> EmailMessage:
    """An invitation into an organisation that already exists.

    The link is the credential — it reached this address and nowhere else, which is the
    same proof a one-time code would produce and the reason acceptance does not send a
    second one. It carries no organisation identifier: an invitee needs the name of the
    organisation to recognise the invitation, and nothing more.
    """
    expiry = f"This invitation expires in {hours} hours and can be used once."
    href = f"{app_url}/pilot/accept?token={TOKEN_SLOT}"
    html = document(
        preheader=f"{organisation} has invited you to JUTSU.",
        sections=[
            ui.header("Invitation"),
            ui.graph_strip(),
            ui.hero(
                f"{organisation} has invited you to JUTSU",
                "JUTSU is your organisation's memory — the decisions, projects and "
                "expertise behind the work, searchable and always cited. Accepting "
                "creates your account and issues your JUTSU ID.",
            ),
            ui.action_button(label="Accept your invitation", href=href),
            ui.detail_card(
                title="Invitation details",
                rows=[("Organisation", organisation), ("Invited address", to)],
            ),
            ui.paragraph(expiry),
            ui.notice(
                title="Security",
                points=[
                    "This link was sent to your address only. Do not forward it — "
                    "whoever opens it becomes the account.",
                    "JUTSU never asks for a password. Signing in sends a one-time code "
                    "to this address.",
                    "If you were not expecting this, ignore the message and the "
                    "invitation will expire on its own.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to} at the request of {organisation}.")),
        ],
    )
    text = text_document(
        heading=f"{organisation} has invited you to JUTSU",
        blocks=[
            "Accepting creates your account and issues your JUTSU ID.",
            f"Accept: {href}",
            expiry,
            "This link was sent to your address only. Do not forward it — whoever "
            "opens it becomes the account. If you were not expecting this, ignore the "
            "message and the invitation will expire on its own.",
        ],
        footer_lines=_footer_lines(f"Sent to {to} at the request of {organisation}."),
    )
    return _compose(
        to=to, subject=f"You have been invited to {organisation} on JUTSU", text=text, html=html
    )


def employee_welcome(
    *, to: str, organisation: str, jutsu_id: str, role: str, app_url: str
) -> EmailMessage:
    """Confirmation that somebody has joined, and the identifier they will be asked for.

    **No organisation identifier, deliberately.** The console asks a returning employee
    for their own JUTSU ID and their address; it never asks for the tenant's. Putting one
    here would place a tenant-scoped value in every employee's inbox to serve a form that
    does not want it.

    No code either: the invitation they just redeemed already proved this mailbox, and
    they hold a session as they read this.
    """
    html = document(
        preheader=f"You have joined {organisation} on JUTSU. Your JUTSU ID is inside.",
        sections=[
            ui.header("Welcome aboard"),
            ui.graph_strip(),
            ui.hero(
                f"You have joined {organisation}",
                "Your JUTSU account is active. Keep the ID below — it is what the "
                "console asks for when you sign in again.",
            ),
            ui.detail_card(
                title="Your account",
                rows=[
                    ("Organisation", organisation),
                    ("Your JUTSU ID", jutsu_id),
                    ("Your role", role),
                    ("Sign in with", to),
                ],
            ),
            ui.action_button(label="Open JUTSU", href=f"{app_url}/me"),
            ui.paragraph(
                "There is no password. Signing in asks for your JUTSU ID and this "
                "address, then sends a one-time code here."
            ),
            ui.notice(
                title="Good to know",
                points=[
                    "Your JUTSU ID identifies you; it does not authenticate you. The "
                    "code sent to this address is what does.",
                    "You will only ever see what your role allows, and every answer "
                    "JUTSU gives you cites the source it came from.",
                    "If you did not expect this, tell your administrator — your "
                    "account can be deactivated immediately.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to} because you joined {organisation}.")),
        ],
    )
    text = text_document(
        heading=f"You have joined {organisation} on JUTSU",
        blocks=[
            "Your JUTSU account is active. Keep the ID below — it is what the console "
            "asks for when you sign in again.",
            (
                f"Organisation:  {organisation}\n"
                f"Your JUTSU ID: {jutsu_id}\n"
                f"Your role:     {role}\n"
                f"Sign in with:  {to}"
            ),
            f"Open JUTSU: {app_url}/me",
            "There is no password. Signing in asks for your JUTSU ID and this address, "
            "then sends a one-time code here. The ID identifies you; the code is what "
            "authenticates you.",
        ],
        footer_lines=_footer_lines(f"Sent to {to} because you joined {organisation}."),
    )
    return _compose(to=to, subject=f"Welcome to {organisation} on JUTSU", text=text, html=html)


def sign_in_code(*, to: str, app_url: str, minutes: int) -> EmailMessage:
    """A returning person's sign-in code. The code, and nothing else.

    **No organisation identifier, no JUTSU ID, no organisation name.** Partly because a
    returning caller needs none of it — they typed their JUTSU ID into the form that
    produced this — and partly because this builder is reached before anyone knows
    whether the address has an account at all. A message that named the organisation
    would answer, in the recipient's inbox, a question the HTTP response is carefully
    built never to answer.
    """
    expiry = f"Expires in {minutes} minutes. One use only."
    html = document(
        preheader="Your JUTSU sign-in code. It expires shortly and can be used once.",
        sections=[
            ui.header("Console sign-in"),
            ui.graph_strip(),
            ui.hero(
                "Your JUTSU sign-in code",
                "Enter this code on the sign-in page to finish opening your console.",
            ),
            ui.code_panel(label="Sign-in code", slot=CODE_SLOT, caption=expiry),
            ui.action_button(label="Open the sign-in page", href=_verify_url(app_url)),
            ui.notice(
                title="Security",
                points=[
                    "This code signs somebody in. JUTSU staff will never ask you for "
                    "it, by phone, chat or email.",
                    "It works once and only from this message. Requesting another one "
                    "immediately invalidates it.",
                    "If you did not ask to sign in, ignore this message — nothing "
                    "happens without the code, and nobody has access to your account.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to} because a sign-in was requested.")),
        ],
    )
    text = text_document(
        heading="Your JUTSU sign-in code",
        blocks=[
            "Enter this code on the sign-in page to finish opening your console:",
            f"    {CODE_SLOT}",
            expiry,
            f"Or open: {_verify_url(app_url)}",
            "JUTSU staff will never ask you for this code. If you did not ask to sign "
            "in, ignore this message — nothing happens without it.",
        ],
        footer_lines=_footer_lines(f"Sent to {to} because a sign-in was requested."),
    )
    return _compose(to=to, subject="Your JUTSU sign-in code", text=text, html=html)


def no_account(*, to: str, app_url: str) -> EmailMessage:
    """Somebody asked to sign in with an address that has no account.

    Sent, rather than skipped, and that is the anti-enumeration control: an address with
    no account causes the same work, the same latency and the same 202 as one with. The
    difference lives here, in the recipient's own inbox, where only they can read it.

    Carries no code and no token — `EmailMessage.secrets` is empty for this one, so there
    is nothing for the transport to substitute even if a slot were left in by mistake.
    """
    html = document(
        preheader="Someone asked to sign in to JUTSU with this address.",
        sections=[
            ui.header("Console sign-in"),
            ui.graph_strip(),
            ui.hero(
                "There is no JUTSU account for this address",
                "Somebody asked to sign in to JUTSU using this email address, but no "
                "account is registered to it.",
            ),
            ui.paragraph(
                "If that was you: your organisation's administrator issues accounts, "
                "so ask them for an invitation. If you are setting JUTSU up for your "
                "company, you can register an organisation yourself."
            ),
            ui.action_button(label="Register an organisation", href=f"{app_url}/pilot"),
            ui.notice(
                title="If this was not you",
                points=[
                    "Nothing has been created and no account exists for this address.",
                    "You can ignore this message. There is nothing to undo.",
                ],
            ),
            ui.rule(),
            ui.footer(lines=_footer_lines(f"Sent to {to} because a sign-in was requested.")),
        ],
    )
    text = text_document(
        heading="There is no JUTSU account for this address",
        blocks=[
            "Somebody asked to sign in to JUTSU using this email address, but no "
            "account is registered to it.",
            "Your organisation's administrator issues accounts, so ask them for an "
            "invitation. If you are setting JUTSU up for your company, you can "
            f"register one at {app_url}/pilot",
            "If this was not you: nothing has been created and there is nothing to undo.",
        ],
        footer_lines=_footer_lines(f"Sent to {to} because a sign-in was requested."),
    )
    # A different subject from the code mail, and safe to differ: a subject reaches
    # nobody but the mailbox that was named, and whoever holds it already knows what
    # they typed. Reusing "Your JUTSU sign-in code" for a message that contains no code
    # is the confusing option, not the private one.
    return _compose(to=to, subject="About your JUTSU sign-in request", text=text, html=html)
