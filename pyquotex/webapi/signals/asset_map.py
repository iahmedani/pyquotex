"""Normalise human-readable asset names to the broker-side symbol.

Quotex symbols look like ``EURUSD`` / ``EURUSD_otc``. Telegram
messages use a zoo of formats:

    NZD/USD (OTC)        -> NZDUSD_otc
    USD/INR (OTC)        -> USDINR_otc
    USDBDT-OTC           -> USDBDT_otc
    USD PHP OTC          -> USDPHP_otc
    GOLD-OTC             -> GOLD_otc
    EURUSD               -> EURUSD
"""
from __future__ import annotations

import re

# Strip everything except letters and digits, then handle the OTC
# suffix in a second pass — keeps the rule cheap and predictable.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def to_quotex_symbol(raw: str) -> str:
    """Return the broker-ready symbol. Case-insensitive on input,
    always lower-cases the ``_otc`` suffix on output."""
    if not raw:
        return ""
    s = raw.strip().upper()
    # Quick OTC test before character stripping so "USD PHP OTC" still
    # matches.
    is_otc = bool(re.search(r"\bOTC\b", s)) or s.endswith("OTC") or s.endswith("-OTC")
    s = _NON_ALNUM.sub("", s)
    if s.endswith("OTC"):
        s = s[:-3]
        is_otc = True
    return f"{s}_otc" if is_otc else s
