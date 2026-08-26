"""The palette and type scale the authentication emails render with.

Hex, not `oklch`. The product's tokens live in `packages/ui/src/tokens.css` and are
authored in `oklch` because browsers interpolate it correctly; no mail client parses it,
and one that cannot parse a colour falls back to the initial value — black text on a
black panel rather than an obviously wrong colour. These values are the same tokens
converted once, here, so the emails and the console are the same green rather than two
greens that drifted apart.

**The document is light regardless of the reader's theme.** Outlook's rendering engine
has no dark mode and Gmail's is an automatic inversion applied to colours it chooses,
not a second palette it asks the message for — authoring a dark email means Gmail
inverts an already-dark design into an unreadable one. So: light ground, dark header
band, and `color-scheme: light` in the head to ask the clients that honour it to leave
the palette alone. The obsidian header is what carries the brand's dark identity across.

`BRAND` is the light-theme green from the token file, which is the one that clears 4.5:1
against white. `BRAND_BRIGHT` is the dark-theme green, used only on the obsidian band
where it is the one that clears instead. Swapping them is the single most likely edit to
make text illegible, which is why they are named for the ground they belong on rather
than for their lightness.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BODY_FONT",
    "BRAND",
    "BRAND_BRIGHT",
    "CONTENT_WIDTH",
    "GRAPH",
    "GRAPH_BRIGHT",
    "HAIRLINE",
    "INK",
    "MONO_FONT",
    "MUTED",
    "OBSIDIAN",
    "PAGE",
    "PANEL",
    "SURFACE",
]

# ---------------------------------------------------------------- colour

#: `--foreground` (light). Body copy.
INK: Final = "#0e1016"
#: `--muted-foreground` (light). Captions and secondary lines. Dark enough to clear
#: 4.5:1 on both the page ground and the panel fill, which the token file's comment
#: notes was the reason it sits where it does.
MUTED: Final = "#484d55"
#: `--background` (light). The ground the card floats on.
PAGE: Final = "#f9fafb"
#: `--surface` (light). The card itself.
SURFACE: Final = "#ffffff"
#: `--muted` (light). Inset panels — the code block, the detail rows.
PANEL: Final = "#f0f1f4"
#: `--hairline` (light) flattened onto white. Mail clients do not composite `oklch`
#: alpha reliably, so the blend is done here rather than expressed as one.
HAIRLINE: Final = "#e6e8ec"

#: `--surface` (dark). The header band, and the only dark ground in the message.
OBSIDIAN: Final = "#0e1115"

#: `--brand` (light). On white and on the panel fill.
BRAND: Final = "#377610"
#: `--brand` (dark). On the obsidian band only.
BRAND_BRIGHT: Final = "#82c933"
#: `--graph` (light) / `--graph` (dark). The connection colour, held low-chroma so the
#: green stays dominant — the same rule the token file states for the product surfaces.
GRAPH: Final = "#00666b"
GRAPH_BRIGHT: Final = "#4fbec0"

# ---------------------------------------------------------------- type

#: No web font. `@font-face` is stripped by Outlook and by Gmail's web client, so a
#: message that depends on one renders in Times New Roman — which looks broken rather
#: than unstyled. The stack asks for each platform's own UI face and lands on a sane
#: sans everywhere else.
BODY_FONT: Final = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
)
#: The one-time code and the identifiers. Monospace matters here beyond taste: a
#: proportional face renders `0`/`O` and `1`/`l` at different widths in every font, and
#: these are values people transcribe by hand.
MONO_FONT: Final = "'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace"

# ---------------------------------------------------------------- metrics

#: 600px is the width every mail client's reading pane is designed around, and the width
#: below which Outlook stops introducing a horizontal scrollbar.
CONTENT_WIDTH: Final = 600
