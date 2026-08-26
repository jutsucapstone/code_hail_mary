"""Branded authentication email.

One template system behind every message JUTSU sends on the authentication path, so all
six share a header, a footer, a code block, a security notice and a palette rather than
being six documents that agree today and disagree in a month.

    theme.py       the palette and type scale, converted once from the product tokens
    components.py  the sections — header, hero, code panel, detail card, notice, footer
    layout.py      the shell both the HTML and the plain-text alternative are poured into
    messages.py    the six concrete messages, and the rules about what each may carry
    assets/        the mark, attached as a related part rather than fetched over https

Import the builders from here; the modules below are the implementation.
"""

from __future__ import annotations

from jutsu_api.emails.components import LOGO_CID
from jutsu_api.emails.messages import (
    CODE_SLOT,
    TOKEN_SLOT,
    employee_invitation,
    employee_welcome,
    no_account,
    organisation_verification,
    organisation_welcome,
    sign_in_code,
)

__all__ = [
    "CODE_SLOT",
    "LOGO_CID",
    "TOKEN_SLOT",
    "employee_invitation",
    "employee_welcome",
    "no_account",
    "organisation_verification",
    "organisation_welcome",
    "sign_in_code",
]
