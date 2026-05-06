"""Persisted user-tunable settings for the signal pipeline.

Stored as JSON next to the broker session so it survives container
restarts. Anything here is mutable from the UI.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(
    os.environ.get(
        "PYQUOTEX_SIGNALS_CONFIG", "settings/signals.json"
    )
).resolve()


@dataclass
class SignalConfig:
    """All UI-tunable settings. Defaults are conservative."""

    # Trade sizing
    amount: float = 1.0
    mtg_levels: int = 1               # 0 disables Martingale
    mtg_multiplier: float = 2.2       # broker payout ~80% → ×2.2 covers loss

    # Default expiry when the message doesn't specify one
    default_expiry_seconds: int = 60

    # Default TZ offset (hours) when the message lacks one. UTC by default.
    default_tz_offset: float = 0.0

    # Parser toggles — which formats to try on each incoming message
    parsers_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "live_single": True,
            "future_bulk": True,
            "live_prep": True,
        }
    )

    # Telegram channel/chat IDs to listen on. Empty = listen to nothing.
    monitored_chat_ids: list[int] = field(default_factory=list)

    # Maximum age (s) of a future signal at receipt — skip if already too old.
    max_signal_age_seconds: int = 60 * 60

    # Minimum lead-time (s) before a future entry; signals closer than
    # this get fired immediately as live trades.
    immediate_threshold_seconds: int = 5

    # Pause everything (UI panic switch).
    paused: bool = False


class ConfigStore:
    """Thread-safe singleton holding the live :class:`SignalConfig`."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config = self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> SignalConfig:
        with self._lock:
            return self._config

    def update(self, patch: dict[str, Any]) -> SignalConfig:
        """Update a subset of fields. Returns the new config."""
        valid = {f.name for f in fields(SignalConfig)}
        with self._lock:
            current = asdict(self._config)
            for k, v in patch.items():
                if k in valid:
                    current[k] = v
            self._config = SignalConfig(**current)
            self._save(self._config)
            return self._config

    def _load(self) -> SignalConfig:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SignalConfig()
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("signals config load failed (%s); using defaults", e)
            return SignalConfig()
        valid = {f.name for f in fields(SignalConfig)}
        return SignalConfig(**{k: v for k, v in data.items() if k in valid})

    def _save(self, cfg: SignalConfig) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(asdict(cfg), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("signals config save failed: %s", e)
