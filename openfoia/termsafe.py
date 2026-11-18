"""Make untrusted text safe to show in a terminal.

Records APIs, crossref sources, imported FollowTheMoney files and the
documents themselves are third-party text. Printed as-is, an entity name can
carry ESC sequences (OSC 52 writes the clipboard, OSC 0 retitles the window,
OSC 8 makes a hyperlink) or, through Rich, markup such as
``[link=https://evil.example]Verify[/link]``. Rich removes BEL and a few other
C0 controls but passes ESC through.

Everything here is pure string work: no I/O, no network.

- :func:`safe_text` removes control and format characters.
- :func:`safe_markup` also escapes Rich markup, for values interpolated into a
  Rich markup string (``rprint(f"... {safe_markup(name)}")``).
- :func:`safe_cell` returns a :class:`rich.text.Text`, which Rich never parses
  as markup, for table cells.
- :func:`safe_json` is ``json.dumps`` whose output never carries a raw C1
  control, bidi override or lone surrogate, while keeping other non-ASCII text
  readable.
- :class:`SafeConsole` is a Rich console that applies :func:`safe_text` to every
  string it renders, as a backstop for any call site that was missed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.text import Text

#: Zero-width non-joiner and joiner. Kept: they are part of Persian, Indic and
#: emoji spelling, and cannot move the cursor or reorder text.
_JOINERS = "\u200c\u200d"
_LINE_SEPARATORS = "\u2028\u2029"


def safe_text(value: Any, *, multiline: bool = False) -> str:
    """*value* as text with control and format characters removed.

    Drops ESC and the other C0 controls, DEL, C1 controls, bidi overrides and
    isolates (and every other Unicode format character except the two
    joiners), lone surrogates, private-use and unassigned code points. Other
    space characters (no-break space, ideographic space) become a plain space,
    so words stay apart.

    With ``multiline=False`` (the default) line breaks and tabs become spaces:
    one value, one line. With ``multiline=True`` newlines and tabs are kept
    (``\\r\\n`` and a lone ``\\r`` become ``\\n``) for text whose layout matters,
    such as a document excerpt.
    """
    text = "" if value is None else str(value)
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for sep in _LINE_SEPARATORS:
            text = text.replace(sep, "\n")
    else:
        text = re.sub(r"[\r\n\t\u2028\u2029]+", " ", text)
    out = []
    for ch in text:
        if ch.isprintable() or ch in _JOINERS or (multiline and ch in "\n\t"):
            out.append(ch)
        elif unicodedata.category(ch) == "Zs":
            out.append(" ")
    return "".join(out)


def safe_markup(value: Any, *, multiline: bool = False) -> str:
    """:func:`safe_text` with Rich markup escaped, for interpolation into markup."""
    return escape(safe_text(value, multiline=multiline))


def safe_cell(value: Any) -> Text:
    """A one-line Rich Text cell (never parsed as markup); Text values are re-cleaned."""
    if isinstance(value, Text):
        plain = safe_text(value.plain)
        return value if plain == value.plain else Text(plain, style=value.style)
    return Text(safe_text(value))


# Characters json.dumps(ensure_ascii=False) leaves raw that a terminal or
# viewer may act on: DEL and C1 controls, line/paragraph separators, format
# characters (bidi overrides, isolates, marks) other than the joiners, and
# lone surrogates (which also cannot be encoded as UTF-8 at all).
_JSON_UNSAFE = re.compile(
    "[\x7f-\x9f\u00ad\u061c\u180e\u200b\u200e\u200f\u2028-\u202e\u2060-\u206f"
    "\ufeff\ufff9-\ufffb\ud800-\udfff]"
)


def safe_json(obj: Any, **kwargs: Any) -> str:
    """``json.dumps(obj, ensure_ascii=False, **kwargs)``, safe to print.

    The unsafe characters above are written as ``\\uXXXX`` escapes, which is
    still valid JSON and decodes to the same value; everything else non-ASCII
    stays readable. (json.dumps already escapes C0 controls.)
    """
    kwargs.setdefault("ensure_ascii", False)
    text = json.dumps(obj, **kwargs)
    return _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


class SafeConsole(Console):
    """A Rich console that removes control characters from every string it renders.

    Strings passed to ``print`` and string table cells go through
    ``render_str``, where Rich parses markup; cleaning there removes terminal
    escapes from all of them, upstream data or not. It cannot tell markup we
    wrote from markup an API sent, so call sites still escape upstream values
    with :func:`safe_markup` or pass :func:`safe_cell` Text objects.
    """

    def render_str(self, text: str, **kwargs: Any) -> Text:  # type: ignore[override]
        return super().render_str(safe_text(text, multiline=True), **kwargs)
