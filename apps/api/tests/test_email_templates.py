"""The branded authentication emails, and the one rule that must never bend.

**An organisation identifier belongs in exactly one message.** Everything else in this
file is presentation and could be argued about; that sentence is a security property, and
`TestWhatEachMessageMayCarry` is written to fail loudly the moment somebody widens a
signature or pastes a tenant id into a template that should not have one.

No database. These are pure functions over strings, and a test that needed Postgres to
prove a template does not contain a UUID would skip on every laptop without Docker —
which is precisely where somebody edits a template.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from jutsu_api.auth_service import ChallengePurpose, _challenge_message
from jutsu_api.config import Settings
from jutsu_api.email import EmailMessage, fill_secrets, secret_slot
from jutsu_api.emails import (
    CODE_SLOT,
    LOGO_CID,
    TOKEN_SLOT,
    employee_invitation,
    employee_welcome,
    no_account,
    organisation_verification,
    organisation_welcome,
    sign_in_code,
)
from jutsu_core.rbac import ROLE_LABELS, Role, role_label

APP = "https://jutsu.example"

#: Deliberately unmistakable. A UUID-shaped value would risk colliding with something in
#: the markup; these cannot appear by accident, so a containment assertion means what it
#: says.
ORG_ID = "ORGANISATION-IDENTIFIER-SENTINEL"
ADMIN_JUTSU_ID = "JUTSU-ADM-SENTINEL"
EMPLOYEE_JUTSU_ID = "JUTSU-EMP-SENTINEL"


def _organisation_verification() -> EmailMessage:
    return organisation_verification(
        to="ada@example.com",
        company_name="Example Analytical",
        company_domain="example.com",
        app_url=APP,
        minutes=10,
    )


def _organisation_welcome() -> EmailMessage:
    return organisation_welcome(
        to="ada@example.com",
        company_name="Example Analytical",
        company_domain="example.com",
        org_id=ORG_ID,
        jutsu_id=ADMIN_JUTSU_ID,
        role="Organisation Owner",
        app_url=APP,
    )


def _employee_invitation() -> EmailMessage:
    return employee_invitation(
        to="charles@example.com", organisation="Example Analytical", app_url=APP, hours=72
    )


def _employee_welcome() -> EmailMessage:
    return employee_welcome(
        to="charles@example.com",
        organisation="Example Analytical",
        jutsu_id=EMPLOYEE_JUTSU_ID,
        role="Member",
        app_url=APP,
    )


def _sign_in_code() -> EmailMessage:
    return sign_in_code(to="ada@example.com", app_url=APP, minutes=10)


def _no_account() -> EmailMessage:
    return no_account(to="nobody@nowhere.example", app_url=APP)


BUILDERS: dict[str, Callable[[], EmailMessage]] = {
    "organisation_verification": _organisation_verification,
    "organisation_welcome": _organisation_welcome,
    "employee_invitation": _employee_invitation,
    "employee_welcome": _employee_welcome,
    "sign_in_code": _sign_in_code,
    "no_account": _no_account,
}


#: The builders unwrapped, for signature inspection. Separate from `BUILDERS` because
#: those helpers bind arguments, and a signature read off a wrapper would prove nothing
#: about the function it calls.
BUILDER_FNS: dict[str, Callable[..., EmailMessage]] = {
    "organisation_verification": organisation_verification,
    "organisation_welcome": organisation_welcome,
    "employee_invitation": employee_invitation,
    "employee_welcome": employee_welcome,
    "sign_in_code": sign_in_code,
    "no_account": no_account,
}


def _both_parts(message: EmailMessage) -> str:
    """Everything a recipient could read, in one string.

    Both halves, always. Asserting an identifier is absent from the HTML while it sits in
    the plain-text alternative would be a test that passes against a leak — and the text
    part is the one a spam filter, a smartwatch and a terminal client actually show.
    """
    return f"{message.html or ''}\n{message.body}"


class TestWhatEachMessageMayCarry:
    """The expected-behaviour table from the brief, asserted rather than described."""

    def test_only_the_organisation_welcome_carries_the_organisation_id(self) -> None:
        """The whole point of the split.

        `organisation_welcome` is sent once, at creation, to an address that has just
        proved a mailbox. Nothing else may reproduce a tenant identifier — least of all
        the sign-in mail, which is the one a returning employee receives forever.
        """
        carrying = {name for name, build in BUILDERS.items() if ORG_ID in _both_parts(build())}
        assert carrying == {"organisation_welcome"}

    def test_no_builder_but_the_welcome_will_even_accept_an_organisation_id(self) -> None:
        """Stronger than checking the output: checking that there is nowhere to put one.

        A containment assertion only catches a leak that already exists. This catches the
        edit *before* it — widening `sign_in_code` with an `org_id` parameter fails here
        while the template is still innocent, which is the point at which it is cheap to
        reconsider.
        """
        for name in BUILDERS:
            parameters = set(inspect.signature(BUILDER_FNS[name]).parameters)
            if name == "organisation_welcome":
                assert "org_id" in parameters
            else:
                assert "org_id" not in parameters, f"{name} gained an organisation id"

    def test_a_returning_sign_in_carries_the_code_and_nothing_that_identifies_anyone(
        self,
    ) -> None:
        """Scenario three: OTP only.

        No organisation id, no JUTSU ID, and no organisation *name* either — this
        template is chosen before anyone has established that the address has an account
        at all, so naming an organisation would answer in the inbox the question the
        identical 202 exists to leave unanswered.
        """
        rendered = _both_parts(_sign_in_code())

        assert CODE_SLOT in rendered
        assert ORG_ID not in rendered
        assert ADMIN_JUTSU_ID not in rendered
        assert EMPLOYEE_JUTSU_ID not in rendered
        assert "Example Analytical" not in rendered

    def test_an_employee_gets_their_own_id_and_not_the_tenants(self) -> None:
        """Scenario two. The console asks a returning employee for their JUTSU ID by
        name, so withholding it would be withholding the thing they are about to be asked
        for. It never asks for the organisation's, so that stays with the administrator.
        """
        rendered = _both_parts(_employee_welcome())

        assert EMPLOYEE_JUTSU_ID in rendered
        assert ORG_ID not in rendered

    def test_an_invitation_carries_a_token_and_no_identifiers(self) -> None:
        rendered = _both_parts(_employee_invitation())

        assert TOKEN_SLOT in rendered
        assert ORG_ID not in rendered
        assert EMPLOYEE_JUTSU_ID not in rendered

    def test_onboarding_verification_predates_every_identifier(self) -> None:
        """Scenario one, first half. Nothing durable exists when this is sent — that is
        the control that stops a domain being claimed by anyone who can type it — so
        there is no identifier in existence to include."""
        rendered = _both_parts(_organisation_verification())

        assert CODE_SLOT in rendered
        assert ORG_ID not in rendered
        assert ADMIN_JUTSU_ID not in rendered
        # The values the registrant typed, echoed so a typo is caught before a tenant is
        # built around it.
        assert "Example Analytical" in rendered
        assert "example.com" in rendered

    def test_onboarding_welcome_carries_both_identifiers_and_no_code(self) -> None:
        """Scenario one, second half — and the reason it is a second message.

        Whoever reads this was signed in by the redemption that created the organisation.
        A fresh one-time code here would be an unrequested live credential sitting in an
        inbox, which is a liability rather than a convenience.
        """
        rendered = _both_parts(_organisation_welcome())

        assert ORG_ID in rendered
        assert ADMIN_JUTSU_ID in rendered
        assert CODE_SLOT not in rendered

    @pytest.mark.parametrize("name", ["organisation_welcome", "employee_welcome", "no_account"])
    def test_a_message_with_no_credential_leaves_no_slot_to_fill(self, name: str) -> None:
        """These three are sent with an empty `secrets` mapping.

        A leftover placeholder would render as the literal text `[[code]]` where a code
        should be — the failure mode the substitution's leftover-append exists to make
        survivable, and one better prevented than survived.
        """
        rendered = _both_parts(BUILDERS[name]())

        assert CODE_SLOT not in rendered
        assert TOKEN_SLOT not in rendered


class TestEveryMessageIsWellFormed:
    @pytest.mark.parametrize("name", sorted(BUILDERS))
    def test_it_has_both_alternatives(self, name: str) -> None:
        """A `multipart/alternative` with no text part is a strong spam signal, and a
        sign-in code in a junk folder is a locked-out customer."""
        message = BUILDERS[name]()

        assert message.html is not None
        assert message.html.startswith("<!DOCTYPE html")
        assert message.body.strip()
        assert "<" not in message.body, "the text alternative is written, not stripped HTML"

    @pytest.mark.parametrize("name", sorted(BUILDERS))
    def test_it_carries_the_mark_inline(self, name: str) -> None:
        """`cid:`, never https. Outlook blocks remote images by default, and an
        authentication email whose branding only appears after the reader clicks
        "display images" looks exactly like the phishing it is trying not to be."""
        message = BUILDERS[name]()

        assert len(message.inline_images) == 1
        assert message.inline_images[0].cid == LOGO_CID
        assert message.inline_images[0].data[:8] == b"\x89PNG\r\n\x1a\n"
        assert f"cid:{LOGO_CID}" in (message.html or "")
        assert "https://jutsu.co.in/jutsu-logo" not in (message.html or "")

    @pytest.mark.parametrize("name", sorted(BUILDERS))
    def test_it_stays_under_the_size_gmail_clips_at(self, name: str) -> None:
        """Gmail truncates a message over ~102KB and hides the rest behind "view entire
        message" — which on a sign-in mail can hide the code itself."""
        message = BUILDERS[name]()
        weight = len(message.html or "") + len(message.body)
        # base64 costs a third on top of the attached bytes.
        weight += sum(len(image.data) for image in message.inline_images) * 4 // 3

        assert weight < 102_000, f"{name} is {weight} bytes and would be clipped"

    @pytest.mark.parametrize("name", sorted(BUILDERS))
    def test_every_link_is_absolute(self, name: str) -> None:
        """An email has no origin to resolve a path against. A relative href in one is a
        dead link, and the previous body's "you can register at /pilot" was exactly
        that."""
        html = BUILDERS[name]().html or ""

        assert 'href="/' not in html
        assert html.count('href="') == html.count(f'href="{APP}')

    @pytest.mark.parametrize("name", sorted(BUILDERS))
    def test_every_link_survives_being_parsed_as_a_url(self, name: str) -> None:
        """Decoded and parsed, not searched for a substring — which is how the bug this
        replaces got through.

        `_verify_url` used to pre-escape its `&` as `&amp;`, and `action_button` escaped
        the href again, so `&amp;amp;` reached the wire. A browser decodes that to a
        literal `&amp;` and reads the second parameter as `amp;flow`, dropping `flow`
        entirely — and `/pilot/verify` needs `flow=register` to know it is completing a
        registration rather than a sign-in. The old assertion was `"flow=register" in
        html`, which `&amp;amp;flow=register` satisfies perfectly.
        """
        html = BUILDERS[name]().html or ""

        for raw in set(re.findall(r'href="([^"]*)"', html)):
            url = urlsplit(unescape(raw))
            assert url.scheme == "https"
            assert "&amp;" not in url.query, f"{raw} is escaped twice"
            for key in parse_qs(url.query, strict_parsing=bool(url.query)):
                assert key.isidentifier(), f"{raw} parses a parameter named {key!r}"

    def test_the_registration_link_actually_carries_the_registration_flow(self) -> None:
        """The specific value that was lost. Named separately from the sweep above,
        because "no parameter is mangled" and "this parameter is present" are different
        claims and only one of them would have caught it."""
        html = _organisation_verification().html or ""

        verify = next(
            urlsplit(unescape(raw))
            for raw in re.findall(r'href="([^"]*)"', html)
            if "/pilot/verify" in raw
        )
        query = parse_qs(verify.query)

        assert query["flow"] == ["register"]
        assert query["token"] == [TOKEN_SLOT]

    def test_a_sign_in_link_does_not_claim_to_be_a_registration(self) -> None:
        """The mirror of the above: `flow` selects a route, and a sign-in code sent to
        the registration endpoint fails the same purpose check in the other direction."""
        html = _sign_in_code().html or ""

        verify = next(
            urlsplit(unescape(raw))
            for raw in re.findall(r'href="([^"]*)"', html)
            if "/pilot/verify" in raw
        )

        assert "flow" not in parse_qs(verify.query)

    def test_user_supplied_text_cannot_break_out_of_the_markup(self) -> None:
        """Company names are typed by whoever registers. An unescaped `<` would end the
        element it landed in, and the values here land inside attributes as well as
        between them."""
        message = organisation_verification(
            to="ada@example.com",
            company_name='</td></table><script>alert(1)</script>"',
            company_domain="example.com",
            app_url=APP,
            minutes=10,
        )

        assert message.html is not None
        assert "<script>" not in message.html
        assert "&lt;script&gt;" in message.html
        assert "</td></table><script>" not in message.html


class TestTemplateSelection:
    """Which message each flow gets, asserted on the branch that decides it.

    `_challenge_message` is pure, so this runs without a database — which matters,
    because the alternative is that the one assertion tying a *flow* to a *template*
    only ever runs in CI. The end-to-end wiring is asserted in `test_registration_flow`
    and `test_auth_routes`; this is the truth table underneath it.
    """

    SETTINGS = Settings(
        email_pepper=b"test-pepper-not-a-real-secret", environment="test", app_url=APP
    )

    def _select(
        self, *, purpose: str, known_account: bool, organisation: tuple[str, str] | None = None
    ) -> tuple[EmailMessage, bool]:
        return _challenge_message(
            address="ada@example.com",
            purpose=purpose,
            settings=self.SETTINGS,
            known_account=known_account,
            organisation=organisation,
        )

    def _message(
        self, *, purpose: str, known_account: bool, organisation: tuple[str, str] | None = None
    ) -> EmailMessage:
        return self._select(
            purpose=purpose, known_account=known_account, organisation=organisation
        )[0]

    def test_registration_gets_the_onboarding_verification(self) -> None:
        message = self._message(
            purpose=ChallengePurpose.REGISTER,
            known_account=True,
            organisation=("Example Analytical", "example.com"),
        )

        assert message.subject == "Verify Example Analytical on JUTSU"
        # Parsed, not searched for. `&amp;amp;flow=register` contains "flow=register"
        # and drops the parameter entirely; that is the bug this file now guards.
        verify = next(
            urlsplit(unescape(raw))
            for raw in re.findall(r'href="([^"]*)"', message.html or "")
            if "/pilot/verify" in raw
        )
        assert parse_qs(verify.query)["flow"] == ["register"]

    def test_a_returning_caller_gets_the_sign_in_code(self) -> None:
        message = self._message(purpose=ChallengePurpose.SIGN_IN, known_account=True)

        assert message.subject == "Your JUTSU sign-in code"
        assert CODE_SLOT in (message.html or "")
        # Parsed, not substring-matched: `"flow" not in html` also matches
        # `overflow:hidden` in the stylesheet, which is a test that can only pass.
        verify = next(
            urlsplit(unescape(raw))
            for raw in re.findall(r'href="([^"]*)"', message.html or "")
            if "/pilot/verify" in raw
        )
        assert "flow" not in parse_qs(verify.query)

    def test_an_unknown_address_gets_the_no_account_message(self) -> None:
        """Sent rather than skipped: an address with no account causes the same work and
        the same 202, and the difference lives only in the recipient's own inbox."""
        message = self._message(purpose=ChallengePurpose.SIGN_IN, known_account=False)

        assert CODE_SLOT not in (message.html or "")
        assert TOKEN_SLOT not in (message.html or "")

    @pytest.mark.parametrize(
        ("purpose", "known_account", "expects_credential"),
        [
            (ChallengePurpose.REGISTER, True, True),
            # The combination that used to be a trap. `stage_registration` always passes
            # True, so this was unreachable — but the caller keyed the secrets off
            # `known_account` alone, so had anything ever issued a registration challenge
            # for an address with no membership, the recipient would have received an
            # email reading `[[code]]` where the code belongs.
            (ChallengePurpose.REGISTER, False, True),
            (ChallengePurpose.SIGN_IN, True, True),
            (ChallengePurpose.SIGN_IN, False, False),
        ],
    )
    def test_a_template_with_placeholders_is_never_chosen_without_the_values(
        self, purpose: str, known_account: bool, expects_credential: bool
    ) -> None:
        """The template and the flag come from one branch, so they cannot disagree.

        Asserted both ways round: a message with slots must be flagged as needing them,
        and a message flagged as needing them must actually have somewhere to put them.
        """
        message, carries = self._select(
            purpose=purpose,
            known_account=known_account,
            organisation=("Example Analytical", "example.com"),
        )
        rendered = _both_parts(message)
        has_slots = CODE_SLOT in rendered or TOKEN_SLOT in rendered

        assert carries is expects_credential
        assert has_slots is carries

    def test_a_registration_with_no_company_refuses_rather_than_guessing(self) -> None:
        """Unreachable from `stage_registration`, which always supplies it. It raises
        rather than falling back because the fallback would deliver a working code under
        the wrong template — a defect that ships, because nothing about it looks broken.
        """
        with pytest.raises(ValueError, match="must name the organisation"):
            self._select(purpose=ChallengePurpose.REGISTER, known_account=True)


class TestRoleLabels:
    def test_every_role_has_one(self) -> None:
        """`role_label` looks up without a fallback, so a role added without a label
        raises here rather than shipping `hr_admin` into customer-facing mail."""
        assert set(ROLE_LABELS) == set(Role)

    @pytest.mark.parametrize(
        ("role", "expected"), [(Role.HR_ADMIN, "HR Admin"), (Role.IT_ADMIN, "IT Admin")]
    )
    def test_the_acronyms_are_not_title_cased(self, role: Role, expected: str) -> None:
        """`"hr_admin".title()` is "Hr Admin", which reads as a typo in a message whose
        whole job is to look legitimate."""
        assert role_label(role) == expected


class TestSecretSubstitution:
    def test_a_slot_is_replaced_where_the_template_asks_for_one(self) -> None:
        filled, leftover = fill_secrets(f"code {CODE_SLOT} end", {"code": "483920"}, as_html=False)

        assert filled == "code 483920 end"
        assert leftover == ""

    def test_a_secret_the_template_never_mentions_is_appended_not_dropped(self) -> None:
        """An email that promises a code and carries none locks its recipient out, and
        the failure is invisible from the sending side."""
        filled, leftover = fill_secrets("no slots here", {"code": "483920"}, as_html=False)

        assert filled == "no slots here"
        assert leftover == "code: 483920"

    def test_html_substitution_escapes_what_it_inserts(self) -> None:
        """The generators produce digits and URL-safe base64, so this escapes nothing
        today. It is here so that stays true of a value chosen later rather than by
        coincidence of the current ones."""
        filled, _ = fill_secrets(f"<p>{CODE_SLOT}</p>", {"code": '"<b>'}, as_html=True)

        assert filled == "<p>&quot;&lt;b&gt;</p>"

    def test_the_slot_spelling_is_shared_with_the_templates(self) -> None:
        """A template whose placeholder disagreed with the substitution by one character
        would render `[[code]]` where the code belongs, and the first person to see it
        would be a customer."""
        assert CODE_SLOT == secret_slot("code")
        assert TOKEN_SLOT == secret_slot("token")
