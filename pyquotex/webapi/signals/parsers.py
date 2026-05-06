"""Telegram message → :class:`Signal` parsers.

Three formats are supported:

1. ``parse_live_single`` — one message containing pair, direction,
   entry time and expiry (the "5 minute(s) until expiration" template).
2. ``parse_future_bulk`` — one message with a header, a date and a
   list of ``HH:MM ASSET CALL/PUT`` rows ("FUTURE SIGNALS" template).
3. ``parse_live_prep`` + sticker direction — two-message live signal
   where the first message carries pair + expiry and the second is a
   👍 / 👎 sticker.

Each returns ``None`` when the message doesn't match the format, so
callers can try the parsers in order without exceptions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from .asset_map import to_quotex_symbol
from .models import Direction
from .timezones import parse_tz_offset, signal_to_utc, today_in_tz

# ── direction emoji / keyword vocab ────────────────────────────────
DIRECTION_TO_SIDE: dict[str, Direction] = {
    "👍": "call",
    "👎": "put",
    "🟢": "call",
    "🔴": "put",
    "🔼": "call",
    "🔽": "put",
    "⬆": "call",
    "⬇": "put",
    "⬆️": "call",
    "⬇️": "put",
}

_RE_LIVE_TZ = re.compile(
    r"(?:time\s*zone|timezone)\s*[:=]?\s*UTC\s*([+\-]\s*\d{1,2}(?::\d{2})?)",
    re.I,
)
_RE_LIVE_EXPIRY = re.compile(
    r"(\d+)\s*(?:minutes?|minutos?|mins?|m)\b", re.I
)
_RE_LIVE_DIRWORD = re.compile(
    r"\b(UP|DOWN|HIGH|LOW|CALL|PUT|BUY|SELL)\b", re.I,
)
# Asset on the same line as the direction keyword. Greedy on the asset
# side: anything from start of line up to the entry time / direction.
_RE_LIVE_LINE = re.compile(
    r"^\s*(?P<asset>[A-Za-z][A-Za-z0-9 /\-()]+?)\s+"
    r"(?:(?P<hh>\d{1,2}):(?P<mm>\d{2}))?\s*"
    r"(?P<dir>UP|DOWN|HIGH|LOW|CALL|PUT|BUY|SELL)\s*"
    r"(?P<emoji>[🟢🔴🔼🔽👍👎⬆⬇️]*)\s*$",
    re.I | re.M,
)

_DIR_WORD_MAP: dict[str, Direction] = {
    "up": "call", "high": "call", "call": "call", "buy": "call",
    "down": "put", "low": "put", "put": "put", "sell": "put",
}

# ── future-bulk regexes ───────────────────────────────────────────
_RE_FUT_HEADER = re.compile(r"future\s+signals", re.I)
_RE_FUT_DATE = re.compile(
    r"date\s*[:=]\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", re.I
)
_RE_FUT_TZ = re.compile(
    r"timezone\s*[:=]\s*UTC/?GMT\s*\(?\s*([+\-])\s*(\d{1,2})(?::?(\d{2}))?\s*\)?",
    re.I,
)
# 23:51 USDBDT-OTC PUT  (allow trailing emoji/extras)
_RE_FUT_ROW = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s+"
    r"(?P<asset>[A-Za-z0-9][A-Za-z0-9 /\-]{1,30}?)\s+"
    r"(?P<dir>CALL|PUT|UP|DOWN|HIGH|LOW)\b",
    re.M | re.I,
)

# ── prep-message regexes (multi-message live) ─────────────────────
_RE_PREP_PAIR = re.compile(
    r"PAIR\s*[:=]\s*(?P<asset>[A-Za-z0-9][A-Za-z0-9 /\-()]+?)"
    r"(?:\s{2,}|[\r\n]|$|⏱|⏰|TIME)",
    re.I | re.S,
)
_RE_PREP_TIME = re.compile(
    r"TIME\s*[:=]\s*(\d+)\s*(?:Minute|min|m)\b", re.I
)


@dataclass
class LiveSingleParsed:
    asset_display: str
    asset_symbol: str
    direction: Direction
    expiry_seconds: int


@dataclass
class PrepParsed:
    asset_display: str
    asset_symbol: str
    expiry_seconds: int


@dataclass
class FutureRow:
    asset_display: str
    asset_symbol: str
    direction: Direction
    entry_utc: datetime


@dataclass
class FutureParsed:
    signal_date: date
    tz_offset: float
    rows: list[FutureRow]


def _normalise_dir(token: str) -> Direction | None:
    return _DIR_WORD_MAP.get(token.lower())


def parse_live_single(text: str, default_expiry: int = 60) -> LiveSingleParsed | None:
    """Parse a single-message live signal (pair + direction +
    expiry on one or two lines).

    Returns ``None`` if no asset+direction pair can be located.
    """
    if not text:
        return None
    # Skip obvious future-bulk messages — they have ``FUTURE SIGNALS``
    # banner and many rows; the future parser should handle those.
    if _RE_FUT_HEADER.search(text):
        return None

    expiry = default_expiry
    m_exp = _RE_LIVE_EXPIRY.search(text)
    if m_exp:
        try:
            expiry = max(15, int(m_exp.group(1)) * 60)
        except ValueError:
            pass

    # Try to find an asset/direction line. Walk the matches in order
    # — the first one with a known direction wins.
    for m in _RE_LIVE_LINE.finditer(text):
        dir_word = m.group("dir") or ""
        emoji = m.group("emoji") or ""
        direction = _normalise_dir(dir_word)
        if direction is None:
            for ch in emoji:
                if ch in DIRECTION_TO_SIDE:
                    direction = DIRECTION_TO_SIDE[ch]
                    break
        if direction is None:
            continue
        raw_asset = m.group("asset").strip()
        # Filter false positives: header text containing the keyword
        # but no real currency pair (e.g. ``"5 minute(s) until expiration"``).
        if len(raw_asset) < 3 or raw_asset.lower().startswith(("time", "expir", "until")):
            continue
        symbol = to_quotex_symbol(raw_asset)
        if not symbol:
            continue
        return LiveSingleParsed(
            asset_display=raw_asset,
            asset_symbol=symbol,
            direction=direction,
            expiry_seconds=expiry,
        )
    return None


def parse_future_bulk(text: str, default_tz_offset: float) -> FutureParsed | None:
    """Parse a multi-row FUTURE SIGNALS message. Returns ``None`` when
    the header / row pattern isn't matched."""
    if not text or not _RE_FUT_HEADER.search(text):
        return None

    m_date = _RE_FUT_DATE.search(text)
    if m_date:
        d, mo, y = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        signal_date = date(y, mo, d)
    else:
        signal_date = today_in_tz(default_tz_offset)

    m_tz = _RE_FUT_TZ.search(text)
    if m_tz:
        sign = m_tz.group(1)
        hh = m_tz.group(2)
        mm = m_tz.group(3) or "00"
        try:
            tz_offset = parse_tz_offset(f"{sign}{hh}:{mm}")
        except ValueError:
            tz_offset = default_tz_offset
    else:
        tz_offset = default_tz_offset

    rows: list[FutureRow] = []
    for m in _RE_FUT_ROW.finditer(text):
        try:
            hh, mm = int(m.group(1)), int(m.group(2))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                continue
        except ValueError:
            continue
        raw_asset = m.group("asset").strip()
        direction = _normalise_dir(m.group("dir"))
        if direction is None:
            continue
        symbol = to_quotex_symbol(raw_asset)
        if not symbol:
            continue
        entry_utc = signal_to_utc(time(hh, mm), signal_date, tz_offset)
        rows.append(FutureRow(
            asset_display=raw_asset,
            asset_symbol=symbol,
            direction=direction,
            entry_utc=entry_utc,
        ))

    if not rows:
        return None
    return FutureParsed(
        signal_date=signal_date, tz_offset=tz_offset, rows=rows
    )


def parse_live_prep(text: str, default_expiry: int = 60) -> PrepParsed | None:
    """Parse a multi-message live PREP (asset + expiry) message — the
    first half of a prep+sticker pair. The matching sticker is
    handled by the controller via :func:`emoji_to_direction`.
    """
    if not text or "PAIR" not in text.upper():
        return None
    m_pair = _RE_PREP_PAIR.search(text)
    if not m_pair:
        return None
    raw = re.sub(r"\s+", " ", m_pair.group("asset")).strip(" -/")
    if not raw:
        return None

    symbol = to_quotex_symbol(raw)
    if not symbol:
        return None

    expiry = default_expiry
    m_t = _RE_PREP_TIME.search(text)
    if m_t:
        try:
            expiry = max(15, int(m_t.group(1)) * 60)
        except ValueError:
            pass

    return PrepParsed(
        asset_display=raw, asset_symbol=symbol, expiry_seconds=expiry,
    )


def emoji_to_direction(text_or_emoji: str) -> Direction | None:
    """Return ``call`` / ``put`` / ``None`` from a sticker alt-text or
    free-form emoji string."""
    if not text_or_emoji:
        return None
    for ch in text_or_emoji:
        if ch in DIRECTION_TO_SIDE:
            return DIRECTION_TO_SIDE[ch]
    return None


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)
