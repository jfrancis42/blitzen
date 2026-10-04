"""Configuration loading for blitzen."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .source import DEFAULT_SOURCE_MASK

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Config:
    #: Home position. Default is Franktown, Colorado.
    lat: float = 39.525445
    lon: float = -104.767522

    #: Strokes farther than this from home are discarded.
    radius_km: float = 100.0

    #: Reporting cadence in minutes -- how often the collector summarises what
    #: it has gathered. This is NOT the network poll interval; see README.
    interval_minutes: float = 5.0

    #: Network poll cadence in seconds. ``None`` follows the server's own hint
    #: (currently 500 ms), which is what keeps the stream gap-free.
    poll_seconds: float | None = None

    #: Source bitmask: 2=Blitzortung, 4=LightningMaps, 8=testing.
    source_mask: int = DEFAULT_SOURCE_MASK

    #: SQLite file. Relative paths resolve against the project directory.
    database: str = "blitzen.db"

    #: Drop stored strokes older than this. 0 disables pruning.
    retain_days: float = 30.0

    # -- protective shutdown ------------------------------------------------

    #: Cut power when lightning is detected within this many statute miles.
    trigger_distance_miles: float = 10.0

    #: Restore power after this many minutes with no strike inside the radius.
    all_clear_minutes: float = 30.0

    #: Treat the feed as blind after this many seconds without a good poll.
    stale_feed_seconds: float = 120.0

    #: While blind, hold the equipment OFF. Turning this off means the gear
    #: stays energised when we cannot see lightning -- rarely what you want.
    fail_safe_on_stale: bool = True

    #: Trip on the near edge of a stroke's uncertainty circle rather than its
    #: point estimate. Network uncertainty is often 2-6 km.
    use_uncertainty_margin: bool = True

    # -- alerting (``blitzen alert``) ------------------------------------------

    #: Announce that the feed is blind once it has been down this long. Shorter
    #: than this is ordinary network noise; longer and a storm can arrive unseen.
    blind_alert_minutes: float = 10.0

    @property
    def trigger_distance_km(self) -> float:
        return self.trigger_distance_miles * 1.609344

    def resolved_database(self, base: Path | None = None) -> Path:
        path = Path(os.path.expanduser(self.database))
        if path.is_absolute():
            return path
        root = base or DEFAULT_CONFIG_PATH.parent
        return root / path

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """Load config from JSON, falling back to built-in defaults.

        A missing file is not an error -- the defaults are a working config.
        """
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            if path:
                raise FileNotFoundError(f"config file not found: {config_path}")
            return cls()

        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)

        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"unknown key(s) in {config_path}: {', '.join(sorted(unknown))}"
            )
        return cls(**data)

    def validate(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"lat out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"lon out of range: {self.lon}")
        if self.radius_km <= 0:
            raise ValueError(f"radius_km must be positive: {self.radius_km}")
        if self.interval_minutes <= 0:
            raise ValueError(f"interval_minutes must be positive: {self.interval_minutes}")
        if self.poll_seconds is not None and self.poll_seconds <= 0:
            raise ValueError(f"poll_seconds must be positive or null: {self.poll_seconds}")
        if not self.source_mask & 0b1110:
            raise ValueError(f"source_mask selects no known source: {self.source_mask}")
        if self.trigger_distance_miles <= 0:
            raise ValueError(
                f"trigger_distance_miles must be positive: {self.trigger_distance_miles}"
            )
        if self.all_clear_minutes <= 0:
            raise ValueError(f"all_clear_minutes must be positive: {self.all_clear_minutes}")
        if self.stale_feed_seconds <= 0:
            raise ValueError(f"stale_feed_seconds must be positive: {self.stale_feed_seconds}")
        if self.blind_alert_minutes <= 0:
            raise ValueError(f"blind_alert_minutes must be positive: {self.blind_alert_minutes}")
        # A trigger radius outside the collection radius would silently never
        # fire: strokes beyond radius_km are discarded before it is consulted.
        if self.trigger_distance_km > self.radius_km:
            raise ValueError(
                f"trigger_distance_miles ({self.trigger_distance_miles:g} mi = "
                f"{self.trigger_distance_km:.1f} km) exceeds radius_km "
                f"({self.radius_km:g} km); widen radius_km or the trigger can never fire"
            )

    def to_dict(self) -> dict:
        return asdict(self)
