"""Telegram-driven signal pipeline.

Layout:

* :mod:`models`      — :class:`Signal` dataclass, parser results
* :mod:`asset_map`   — symbol normalisation (``USD/INR (OTC)`` → ``USDINR_otc``)
* :mod:`timezones`   — UTC offset parsing + signal-time → UTC conversion
* :mod:`parsers`     — three text parsers (live single, future single,
                       multi-message live prep + sticker)
* :mod:`config`      — persisted user settings (channels, amount, MTG,
                       TZ offset, parser toggles)
* :mod:`store`       — bounded in-memory ring buffers for received
                       signals + executed trades; the UI polls these
* :mod:`dispatcher`  — trade execution + Martingale recovery
* :mod:`scheduler`   — fires future-signal trades at the right UTC time
* :mod:`controller`  — wires a Telethon client into the parsers,
                       dispatcher and scheduler; one per process
"""
