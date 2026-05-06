"""Cover the three Telegram signal-message parsers against the
verbatim sample messages provided when this feature was specced.
"""
from __future__ import annotations

from pyquotex.webapi.signals.asset_map import to_quotex_symbol
from pyquotex.webapi.signals.parsers import (
    emoji_to_direction,
    parse_future_bulk,
    parse_live_prep,
    parse_live_single,
)


def test_asset_map_normalises_otc_pairs() -> None:
    cases = {
        "NZD/USD (OTC)": "NZDUSD_otc",
        "USD/INR (OTC)": "USDINR_otc",
        "USDBDT-OTC":     "USDBDT_otc",
        "USD PHP OTC":    "USDPHP_otc",
        "GOLD-OTC":       "GOLD_otc",
        "EURUSD":         "EURUSD",
    }
    for raw, expected in cases.items():
        assert to_quotex_symbol(raw) == expected, raw


def test_live_single_message_low() -> None:
    msg = (
        "⏰ Time zone: UTC -3\n"
        "💰 5 minute(s) until expiration\n\n"
        "NZD/USD (OTC) 15:00 LOW 🔴\n"
    )
    parsed = parse_live_single(msg, default_expiry=60)
    assert parsed is not None
    assert parsed.asset_symbol == "NZDUSD_otc"
    assert parsed.direction == "put"
    assert parsed.expiry_seconds == 300


def test_live_single_message_up_plural_minutes() -> None:
    msg = (
        "🌎 Time zone: UTC -3\n"
        "⏳ 5 minutes until expiration\n"
        "USD/INR (OTC) 13:30 UP 🟢\n"
    )
    parsed = parse_live_single(msg, default_expiry=60)
    assert parsed is not None
    assert parsed.asset_symbol == "USDINR_otc"
    assert parsed.direction == "call"
    assert parsed.expiry_seconds == 300


def test_future_bulk_parses_seven_rows() -> None:
    msg = (
        "DATE: 07.05.2026\n"
        "TIMEZONE : UTC/GMT (+06:00)\n"
        "FUTURE SIGNALS 🕯\n"
        "📏📏📏📏📏📏📏📏📏📏\n\n"
        "23:51 USDBDT-OTC  PUT✅\n"
        "23:55 USDBDT-OTC  PUT✅\n"
        "23:56 USDBDT-OTC  PUT 🎮\n"
        "23:59 USDBDT-OTC CALL✅\n"
        "00:04 USDBDT-OTC CALL\n"
        "00:09 USDBDT-OTC CALL\n"
        "00:14 USDBDT-OTC CALL\n"
    )
    parsed = parse_future_bulk(msg, default_tz_offset=0.0)
    assert parsed is not None
    assert parsed.tz_offset == 6.0
    assert len(parsed.rows) == 7
    assert parsed.rows[0].asset_symbol == "USDBDT_otc"
    assert parsed.rows[0].direction == "put"
    # 23:51 +06:00 is 17:51 UTC
    assert parsed.rows[0].entry_utc.hour == 17
    assert parsed.rows[0].entry_utc.minute == 51
    # last three are CALL
    assert all(r.direction == "call" for r in parsed.rows[-3:])


def test_live_prep_extracts_pair_and_expiry() -> None:
    msg = (
        "🌐 PAIR: USD PHP OTC\n\n"
        "⏱️ TIME: 01 Minute\n\n"
        "🚨 IF LOSS TAKE 1 STEP MTG\n"
    )
    parsed = parse_live_prep(msg, default_expiry=60)
    assert parsed is not None
    assert parsed.asset_symbol == "USDPHP_otc"
    assert parsed.expiry_seconds == 60


def test_emoji_to_direction() -> None:
    assert emoji_to_direction("👍") == "call"
    assert emoji_to_direction("👎") == "put"
    assert emoji_to_direction("🟢") == "call"
    assert emoji_to_direction("🔴") == "put"
    assert emoji_to_direction("🤷") is None
    assert emoji_to_direction("") is None


def test_live_single_returns_none_for_future_bulk_text() -> None:
    """The future-bulk template has a ``FUTURE SIGNALS`` banner; the
    live-single parser must yield ``None`` so the controller reaches
    the future-bulk parser."""
    msg = "FUTURE SIGNALS 🕯\n23:51 USDBDT-OTC PUT\n"
    assert parse_live_single(msg) is None


def test_future_bulk_returns_none_without_header() -> None:
    msg = "23:51 USDBDT-OTC PUT\n"
    assert parse_future_bulk(msg, default_tz_offset=0.0) is None
