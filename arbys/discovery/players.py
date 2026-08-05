"""Player-name parsing for tennis discovery.

Tennis has no fixed roster small enough to enumerate, so we resolve players
dynamically from venue titles. Both Kalshi and Polymarket express matches as
"<player A> vs[.] <player B>". Kalshi uses last name only ("Kecmanovic vs
Rinderknech"), Polymarket uses the full name ("Miomir Kecmanovic vs Arthur
Rinderknech").

Canonical player ``code`` = last-name, uppercased, ASCII-folded (diacritics
stripped). Matching relies on the code so "Á" vs "A" doesn't split a game.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Player:
    code: str  # ASCII, uppercase last name
    full_name: str

    @property
    def last_name(self) -> str:
        return self.code.title()


_VS_SPLIT = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def _normalize(text: str) -> str:
    # Replace letters whose diacritics aren't decomposed by NFKD (e.g. Đ→D).
    replacements = {"Đ": "D", "đ": "d", "Ø": "O", "ø": "o", "Ł": "L", "ł": "l"}
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    stripped = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return ascii_only.strip()


def _last_name_code(full_name: str) -> str:
    cleaned = _normalize(full_name)
    if not cleaned:
        return ""
    # Take everything after the last space as the surname. Handles multi-word
    # first names like "Karen Khachanov" -> "KHACHANOV". If the venue only
    # gives one word (Kalshi), that word IS the surname.
    parts = cleaned.split()
    if not parts:
        return ""
    # Strip common suffixes like "Jr." — rare in tennis but harmless.
    return parts[-1].upper()


def parse_vs_title(title: str) -> tuple[Player, Player] | None:
    """Parse "A vs B" or "A vs. B" into two Players.

    Returns None if the string doesn't contain a recognizable separator or
    either side is empty.
    """
    if not title:
        return None
    parts = _VS_SPLIT.split(title.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    a_full, b_full = parts[0].strip(), parts[1].strip()
    a_code, b_code = _last_name_code(a_full), _last_name_code(b_full)
    if not a_code or not b_code or a_code == b_code:
        return None
    return (
        Player(code=a_code, full_name=a_full or a_code.title()),
        Player(code=b_code, full_name=b_full or b_code.title()),
    )


def strip_prefix(title: str, prefixes: tuple[str, ...] = ()) -> str:
    """Drop leading "<Tournament>: " context Polymarket often prepends.

    Example: "National Bank Open: Foo vs Bar" -> "Foo vs Bar"
    """
    if not title:
        return title
    if ":" in title:
        _head, _, tail = title.partition(":")
        if tail.strip():
            return tail.strip()
    for p in prefixes:
        if title.lower().startswith(p.lower()):
            return title[len(p):].lstrip(" -:")
    return title
