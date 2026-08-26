"""The document the sections are poured into, in both HTML and plain text.

One shell, six messages. Everything a mail client needs before it will render anything
at all — the doctype it recognises, the MSO width lock, the mobile media query, the
`color-scheme` declaration — lives here, so a new authentication email inherits all of it
by existing rather than by remembering to copy it.

**Every message carries a plain-text alternative, and it is not a courtesy.** A
`multipart/alternative` with no text part is one of the strongest spam signals there is,
and this is mail that has to arrive: a sign-in code in a junk folder is a locked-out
customer. The text part is also what a screen reader in text mode, a terminal client and
a smartwatch preview actually read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape

from jutsu_api.emails.theme import (
    BODY_FONT,
    CONTENT_WIDTH,
    HAIRLINE,
    PAGE,
    SURFACE,
)

__all__ = ["document", "text_document"]

#: Zero-width joiners padding the preview line.
#:
#: Gmail and Apple Mail show the first text they find after the subject. Without a
#: preheader that is whatever the header band contains — "JUTSU Organisation setup" —
#: and without the padding the client fills the rest of the preview with the words that
#: follow it. The joiners are invisible, take no space, and consume the remaining preview
#: budget so the line ends where it was written to end.
_PREVIEW_PAD = "&#847;&zwnj;&nbsp;" * 60


def document(*, preheader: str, sections: Sequence[str]) -> str:
    """Wrap rendered sections in the outer shell.

    The MSO conditional table is what stops Outlook stretching the card to the full width
    of a maximised window: Word's engine ignores `max-width`, so the 600px bound has to be
    expressed as a fixed-width table it *does* honour, hidden from everything else.

    `color-scheme: light` and its `<meta>` twin ask the clients that support them not to
    auto-invert. Gmail and Outlook.com invert anyway on some platforms, which is why the
    palette is light to begin with — an inverted light design stays legible, an inverted
    dark one does not.
    """
    body = "".join(sections)
    return (
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"https://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
        '<html xmlns="https://www.w3.org/1999/xhtml" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" lang="en">'
        "<head>"
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width,initial-scale=1" />'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge" />'
        '<meta name="color-scheme" content="light" />'
        '<meta name="supported-color-schemes" content="light" />'
        "<title>JUTSU</title>"
        "<!--[if mso]>"
        "<xml><o:OfficeDocumentSettings>"
        "<o:PixelsPerInch>96</o:PixelsPerInch>"
        "</o:OfficeDocumentSettings></xml>"
        "<![endif]-->"
        "<style>"
        ":root{color-scheme:light;supported-color-schemes:light;}"
        # Outlook.com rewrites class names but keeps the rules; these two are the
        # long-standing fixes for its forced line-height and for Apple's phone-number
        # autolinking, which would turn a six-digit code into a tappable telephone link.
        "body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}"
        "table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}"
        "img{-ms-interpolation-mode:bicubic;border:0;line-height:100%;outline:none;"
        "text-decoration:none;}"
        "a[x-apple-data-detectors]{color:inherit!important;text-decoration:none!important;"
        "font-size:inherit!important;font-family:inherit!important;"
        "font-weight:inherit!important;line-height:inherit!important;}"
        f"@media only screen and (max-width:{CONTENT_WIDTH}px){{"
        ".jutsu-card{width:100%!important;border-radius:0!important;}"
        ".jutsu-pad{padding-left:20px!important;padding-right:20px!important;}"
        ".jutsu-shell{padding:0!important;}"
        "}"
        "</style>"
        "</head>"
        f'<body style="margin:0;padding:0;background-color:{PAGE};'
        f'-webkit-font-smoothing:antialiased;">'
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:{PAGE};opacity:0;">'
        f"{escape(preheader)}{_PREVIEW_PAD}</div>"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{PAGE}" style="background-color:{PAGE};">'
        f'<tr><td align="center" class="jutsu-shell" style="padding:32px 12px;">'
        f'<!--[if mso]><table role="presentation" width="{CONTENT_WIDTH}" '
        f'cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->'
        f'<table role="presentation" width="{CONTENT_WIDTH}" cellpadding="0" '
        f'cellspacing="0" border="0" class="jutsu-card" bgcolor="{SURFACE}" '
        f'style="width:{CONTENT_WIDTH}px;max-width:{CONTENT_WIDTH}px;'
        f"background-color:{SURFACE};border:1px solid {HAIRLINE};border-radius:14px;"
        f'overflow:hidden;font-family:{BODY_FONT};">'
        f"{body}"
        f"</table>"
        f"<!--[if mso]></td></tr></table><![endif]-->"
        f"</td></tr></table>"
        f"</body></html>"
    )


def text_document(*, heading: str, blocks: Sequence[str], footer_lines: Sequence[str]) -> str:
    """The plain-text alternative.

    Written rather than derived. Stripping tags out of the HTML produces something that
    technically parses and reads like a transcript of a table, and the text part is what
    a spam filter scores when it decides whether the HTML is hiding something.
    """
    rule = "-" * 58
    parts = [f"JUTSU\n{rule}\n", f"{heading}\n"]
    parts.extend(f"{block}\n" for block in blocks)
    parts.append(rule)
    parts.extend(footer_lines)
    # A regex rather than `.replace("\n\n\n", "\n\n")`, which is what this was: `str.replace`
    # does not rescan what it has already written, so four newlines collapsed to three and
    # stayed there. The templates do not currently produce a run that long — which is
    # exactly why a half-working guard would have gone unnoticed until one did.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip() + "\n"
