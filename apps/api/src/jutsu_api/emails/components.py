"""The sections an authentication email is assembled from.

Each function returns one `<tr>` of the card table, so a message is a list of these in
order rather than a bespoke document. That is the whole point: six messages that share a
header, a footer, a code block and a notice cannot drift apart if there is one
implementation of each — and the alternative, six hand-written HTML files, drifts within
a fortnight because a change gets made in three of them.

**Everything is a table, and every style is inline.** Not nostalgia: Outlook for Windows
renders HTML through Word, which has no flexbox, no grid, no `float` worth relying on and
no `<style>` support for anything Gmail's mobile clients will also honour. Layout that
survives all of them is nested tables with `role="presentation"` — the role is what keeps
a screen reader from announcing the message as a data table with forty cells.

**Nothing here interpolates without escaping.** Company names, display names and domains
all come from user input, and a `<` in an organisation's name would otherwise end the
element it landed in. `_esc` is applied at every boundary rather than at the call sites,
because a call site that forgets is invisible until someone registers with an apostrophe
in their company name.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape

from jutsu_api.emails.theme import (
    BODY_FONT,
    BRAND,
    BRAND_BRIGHT,
    GRAPH,
    GRAPH_BRIGHT,
    HAIRLINE,
    INK,
    MONO_FONT,
    MUTED,
    OBSIDIAN,
    PANEL,
    SURFACE,
)

__all__ = [
    "LOGO_CID",
    "action_button",
    "code_panel",
    "detail_card",
    "footer",
    "graph_strip",
    "header",
    "hero",
    "notice",
    "paragraph",
    "rule",
]

#: The Content-ID the transport attaches the mark under. Referenced as `cid:` rather
#: than as an https URL because Outlook blocks remote images by default and Gmail
#: blocks them for any sender the reader has not corresponded with — a sign-in email
#: whose branding only appears after the recipient clicks "display images" looks
#: exactly like the phishing it is trying not to be mistaken for. A related part is
#: not a remote fetch, so it renders on first open everywhere.
LOGO_CID = "jutsu-mark"


def _esc(value: object) -> str:
    """HTML-escape anything on its way into markup, quotes included.

    `quote=True` matters more than it looks: several of these values land inside
    attributes (`alt`, `title`), and an unescaped `"` there ends the attribute and turns
    the rest of the value into markup.
    """
    return escape(str(value), quote=True)


def _row(content: str) -> str:
    return f"<tr>{content}</tr>"


def header(eyebrow: str) -> str:
    """The obsidian band: the mark, the wordmark, and what this message is about.

    The eyebrow is the one line that differs between an onboarding mail and a sign-in
    mail, and it sits in the band rather than in the body so a reader scanning an inbox
    preview can tell the two apart before opening either.
    """
    return _row(
        f'<td bgcolor="{OBSIDIAN}" class="jutsu-pad" '
        f'style="background-color:{OBSIDIAN};padding:26px 32px;'
        f'border-radius:14px 14px 0 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td align="left" valign="middle" width="44" style="width:44px;">'
        # The tile is white and the PNG is composited on white, so the two meet without
        # a seam. Width and height are on the element as well as in the style: Outlook
        # ignores CSS dimensions on images and reads the attributes.
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="{SURFACE}" style="background-color:{SURFACE};border-radius:10px;'
        f'padding:6px;line-height:0;">'
        f'<img src="cid:{LOGO_CID}" width="32" height="32" alt="JUTSU" '
        f'style="display:block;width:32px;height:32px;border:0;outline:none;'
        f'text-decoration:none;" />'
        f"</td></tr></table>"
        f"</td>"
        f'<td align="left" valign="middle" style="padding-left:14px;font-family:{BODY_FONT};">'
        f'<div style="font-family:{BODY_FONT};font-size:17px;font-weight:700;'
        f'letter-spacing:0.18em;color:{SURFACE};line-height:1.1;">JUTSU</div>'
        f'<div style="font-family:{MONO_FONT};font-size:10px;font-weight:500;'
        f"letter-spacing:0.16em;text-transform:uppercase;color:{BRAND_BRIGHT};"
        f'padding-top:5px;line-height:1.1;">{_esc(eyebrow)}</div>'
        f"</td></tr></table>"
        f"</td>"
    )


def graph_strip() -> str:
    """The one piece of decoration, and it is the product's own diagram.

    The memory graph: five nodes and the four edges between them. Drawn from table cells
    rather than an image so it costs nothing to attach, never triggers image blocking and
    stays sharp on every display. Outlook squares off the `border-radius`, which turns the
    nodes into small tiles; the figure still reads as a graph, which is why that is
    acceptable here where it would not be on a button.
    """
    node = (
        '<td width="10" style="width:10px;line-height:0;font-size:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="10"><tr>'
        '<td bgcolor="{fill}" height="10" style="background-color:{fill};height:10px;'
        'width:10px;border-radius:5px;line-height:0;font-size:0;">&nbsp;</td>'
        "</tr></table></td>"
    )
    edge = (
        '<td style="line-height:0;font-size:0;padding:0 6px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%"><tr>'
        '<td bgcolor="{fill}" height="2" style="background-color:{fill};height:2px;'
        'line-height:0;font-size:0;">&nbsp;</td>'
        "</tr></table></td>"
    )
    fills = (BRAND_BRIGHT, GRAPH_BRIGHT, GRAPH_BRIGHT, BRAND_BRIGHT, GRAPH_BRIGHT)
    cells = []
    for index, fill in enumerate(fills):
        if index:
            cells.append(edge.format(fill=GRAPH))
        cells.append(node.format(fill=fill))

    return _row(
        f'<td bgcolor="{OBSIDIAN}" class="jutsu-pad" '
        f'style="background-color:{OBSIDIAN};padding:0 32px 26px 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr>{"".join(cells)}</tr></table>'
        f"</td>"
    )


def hero(title: str, lead: str) -> str:
    """The headline and the one sentence under it.

    26px rather than something larger: mail clients on phones do not honour
    `text-wrap: balance`, so a display size that looks right at 600px breaks into four
    ragged lines at 320px.
    """
    return _row(
        f'<td class="jutsu-pad" style="padding:34px 32px 0 32px;font-family:{BODY_FONT};">'
        f'<h1 style="margin:0;font-family:{BODY_FONT};font-size:26px;line-height:1.24;'
        f'font-weight:700;letter-spacing:-0.02em;color:{INK};">{_esc(title)}</h1>'
        f'<p style="margin:14px 0 0 0;font-family:{BODY_FONT};font-size:15px;'
        f'line-height:1.62;color:{MUTED};">{_esc(lead)}</p>'
        f"</td>"
    )


def paragraph(text: str) -> str:
    return _row(
        f'<td class="jutsu-pad" style="padding:18px 32px 0 32px;font-family:{BODY_FONT};">'
        f'<p style="margin:0;font-family:{BODY_FONT};font-size:15px;line-height:1.62;'
        f'color:{MUTED};">{_esc(text)}</p>'
        f"</td>"
    )


def code_panel(*, label: str, slot: str, caption: str) -> str:
    """The one-time code, and the largest thing in the message.

    `slot` is a placeholder — `[[code]]` — not the code. The transport substitutes it at
    render time, which is what keeps `EmailMessage.secrets` the only object that ever
    holds the value and keeps every template safe to log, diff and snapshot.

    Letter-spaced monospace at 34px because this is transcribed by hand, often from a
    phone held in the other hand. `user-select` and the `mso-` line-height rule are
    there so a reader can also just copy it.
    """
    return _row(
        f'<td class="jutsu-pad" style="padding:28px 32px 0 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{PANEL}" '
        f'style="background-color:{PANEL};border-radius:12px;border:1px solid {HAIRLINE};">'
        f'<tr><td align="center" style="padding:22px 20px 20px 20px;">'
        f'<div style="font-family:{MONO_FONT};font-size:10px;font-weight:600;'
        f'letter-spacing:0.16em;text-transform:uppercase;color:{MUTED};">{_esc(label)}</div>'
        f'<div style="font-family:{MONO_FONT};font-size:34px;font-weight:700;'
        f"letter-spacing:0.28em;color:{INK};padding:14px 0 0 0;line-height:1.1;"
        f'mso-line-height-rule:exactly;">'
        # The trailing letter-spacing on the last glyph pushes the run off centre by
        # half a space; the indent cancels it. Visible at this size, invisible at 14px.
        f'<span style="margin-left:0.28em;">{slot}</span></div>'
        f'<div style="font-family:{BODY_FONT};font-size:13px;line-height:1.5;'
        f'color:{MUTED};padding:14px 0 0 0;">{_esc(caption)}</div>'
        f"</td></tr></table>"
        f"</td>"
    )


def detail_card(*, title: str, rows: Sequence[tuple[str, str]]) -> str:
    """Label/value pairs — an organisation's identity, or a person's.

    Two columns rather than a definition list: Outlook collapses `<dl>` margins
    unpredictably, and a table lets the label column hold its width so the values line
    up. Values render monospace because every one of them is an identifier someone will
    compare character by character.
    """
    body = "".join(
        f'<tr><td align="left" valign="top" '
        f'style="padding:{"14px" if index else "0"} 0 0 0;font-family:{BODY_FONT};'
        f'font-size:12px;line-height:1.5;color:{MUTED};white-space:nowrap;">'
        f"{_esc(label)}</td>"
        f'<td align="right" valign="top" '
        f'style="padding:{"14px" if index else "0"} 0 0 12px;font-family:{MONO_FONT};'
        f"font-size:13px;line-height:1.5;font-weight:600;color:{INK};"
        f'word-break:break-all;">{_esc(value)}</td></tr>'
        for index, (label, value) in enumerate(rows)
    )

    return _row(
        f'<td class="jutsu-pad" style="padding:28px 32px 0 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="border:1px solid {HAIRLINE};border-radius:12px;">'
        f'<tr><td style="padding:18px 20px 20px 20px;">'
        f'<div style="font-family:{MONO_FONT};font-size:10px;font-weight:600;'
        f"letter-spacing:0.16em;text-transform:uppercase;color:{BRAND};"
        f'padding-bottom:16px;">{_esc(title)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{body}</table>'
        f"</td></tr></table>"
        f"</td>"
    )


def action_button(*, label: str, href: str) -> str:
    """A filled call to action that survives Outlook.

    The VML conditional is not optional decoration. Word's engine ignores padding on an
    anchor, so without it the "button" collapses to underlined text with a coloured
    background one line tall. `v:roundrect` draws the shape natively there and is hidden
    from every other client; the anchor inside the `<!--[if !mso]>` half is what those
    clients render.

    `href` is escaped like everything else — it carries a token placeholder the transport
    fills in, and the substitution escapes what it inserts too.
    """
    safe_href = _esc(href)
    return _row(
        f'<td align="center" class="jutsu-pad" style="padding:26px 32px 0 32px;">'
        f"<!--[if mso]>"
        f'<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" '
        f'xmlns:w="urn:schemas-microsoft-com:office:word" href="{safe_href}" '
        f'style="height:46px;v-text-anchor:middle;width:280px;" arcsize="26%" '
        f'stroke="f" fillcolor="{BRAND}">'
        f'<w:anchorlock/><center style="color:{SURFACE};font-family:{BODY_FONT};'
        f'font-size:15px;font-weight:600;">{_esc(label)}</center>'
        f"</v:roundrect>"
        f"<![endif]-->"
        f"<!--[if !mso]><!-- -->"
        f'<a href="{safe_href}" '
        f'style="display:inline-block;background-color:{BRAND};color:{SURFACE};'
        f"font-family:{BODY_FONT};font-size:15px;font-weight:600;line-height:1;"
        f"text-decoration:none;padding:16px 30px;border-radius:12px;"
        f'mso-hide:all;">{_esc(label)}</a>'
        f"<!--<![endif]-->"
        f"</td>"
    )


def notice(*, title: str, points: Iterable[str]) -> str:
    """The security block. A left rule in brand green rather than a red alert box.

    Deliberately not styled as a warning. These messages are sent on the happy path —
    somebody asked for a code and got one — and a red panel on every sign-in trains the
    reader to ignore it, which is exactly the opposite of what it is for. It reads as
    important, once, and stays readable.
    """
    items = "".join(
        f'<tr><td valign="top" style="padding:{"9px" if index else "0"} 8px 0 0;'
        f'font-family:{BODY_FONT};font-size:13px;line-height:1.6;color:{BRAND};">&bull;</td>'
        f'<td valign="top" style="padding:{"9px" if index else "0"} 0 0 0;'
        f'font-family:{BODY_FONT};font-size:13px;line-height:1.6;color:{MUTED};">'
        f"{_esc(point)}</td></tr>"
        for index, point in enumerate(points)
    )

    return _row(
        f'<td class="jutsu-pad" style="padding:28px 32px 0 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{PANEL}" style="background-color:{PANEL};border-radius:12px;">'
        f'<tr><td width="3" bgcolor="{BRAND}" '
        f'style="width:3px;background-color:{BRAND};line-height:0;font-size:0;'
        f'border-radius:12px 0 0 12px;">&nbsp;</td>'
        f'<td style="padding:18px 20px 20px 17px;">'
        f'<div style="font-family:{BODY_FONT};font-size:13px;font-weight:700;'
        f'color:{INK};padding-bottom:10px;">{_esc(title)}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f"{items}</table>"
        f"</td></tr></table>"
        f"</td>"
    )


def rule() -> str:
    return _row(
        f'<td class="jutsu-pad" style="padding:32px 32px 0 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0"><tr><td height="1" bgcolor="{HAIRLINE}" '
        f'style="height:1px;background-color:{HAIRLINE};line-height:0;font-size:0;">'
        f"&nbsp;</td></tr></table>"
        f"</td>"
    )


def footer(*, lines: Sequence[str]) -> str:
    """Who sent this and why, plus the line that says not to reply.

    Inside the card rather than below it. A footer floating on the page ground is the
    convention for marketing mail and reads as one; an authentication message that looks
    like a newsletter is a message people trust less.
    """
    body = "".join(
        f'<p style="margin:{"7px" if index else "0"} 0 0 0;font-family:{BODY_FONT};'
        f'font-size:12px;line-height:1.6;color:{MUTED};">{_esc(line)}</p>'
        for index, line in enumerate(lines)
    )
    return _row(f'<td class="jutsu-pad" style="padding:22px 32px 30px 32px;">{body}</td>')
