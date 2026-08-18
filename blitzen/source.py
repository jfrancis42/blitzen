"""Client for the LightningMaps.org live stroke feed.

Transport notes (reverse-engineered from the site's own ``js/realtime.js``,
2026-08-17 — there is no documented public API; docs.lightningmaps.org lists
its API section as "work in progress"):

* Endpoint: ``https://live.lightningmaps.org/l/`` (``live2`` is the alternate).
* Query parameters:
    ``v``  protocol version the site currently sends (24)
    ``l``  cursor -- echo back the ``s`` value from the previous response
    ``i``  source bitmask: 2=Blitzortung, 4=LightningMaps, 8=testing
* The feed is a *continuous cursor stream*, not a time-range query. Passing
  ``l=0`` primes the cursor and returns no strokes; each later poll returns the
  strokes appended since that cursor. The server tells you how long to wait via
  ``w``/``o`` (milliseconds).

Response fields, and how the site interprets them:

    ``t``    reference epoch second for this response
    ``d``    list of strokes
    ``s``    new cursor
    ``w``/``o``  requested wait before the next poll, in ms

Per stroke:

    ``time``  milliseconds relative to ``t``; absolute = ``t + time/1000``
    ``lat``/``lon``  decimal degrees, as strings
    ``dev``   location uncertainty in metres (the map draws a circle of ``dev/2``)
    ``del``   detection delay in milliseconds
    ``src``   source id; the mask bit for source ``n`` is ``1 << n``
    ``id``    per-source stroke id (wraps periodically -- not globally unique)
    ``srv``   originating server id
    ``alt``   altitude, when present
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

DEFAULT_HOSTS = ("live.lightningmaps.org", "live2.lightningmaps.org")
PROTOCOL_VERSION = 24

#: ``i=`` bitmask values, keyed by the names the site's own UI uses.
SOURCE_MASKS = {"blitzortung": 2, "lightningmaps": 4, "testing": 8}
#: Both production networks. The website defaults to 4 alone.
DEFAULT_SOURCE_MASK = SOURCE_MASKS["blitzortung"] | SOURCE_MASKS["lightningmaps"]

SOURCE_NAMES = {1: "blitzortung", 2: "lightningmaps", 3: "testing"}

USER_AGENT = "blitzen/0.1 (personal non-commercial lightning monitor)"

# Guard rails on the server's own wait hint.
MIN_WAIT_S = 0.5
MAX_WAIT_S = 60.0


@dataclass(frozen=True)
class Stroke:
    """One located lightning stroke."""

    src: int
    stroke_id: int
    time_utc: float
    lat: float
    lon: float
    dev_m: float | None = None
    delay_ms: int | None = None
    alt_m: float | None = None
    server: int | None = None

    @property
    def source_name(self) -> str:
        return SOURCE_NAMES.get(self.src, f"src{self.src}")

    @property
    def dedup_key(self) -> tuple[int, int, int]:
        """Identity for de-duplication.

        ``id`` alone is not safe: it is per-source and wraps, so it is combined
        with the source and the whole-millisecond timestamp.
        """
        return (self.src, self.stroke_id, int(self.time_utc * 1000))


@dataclass
class PollResult:
    strokes: list[Stroke] = field(default_factory=list)
    wait_s: float = 1.0
    server_time: float | None = None
    cursor: int | None = None
    raw_keys: tuple[str, ...] = ()


class LightningMapsError(RuntimeError):
    pass


class LightningMapsSource:
    """Cursor-following reader for the live stroke feed.

    The instance is stateful: it remembers the cursor between :meth:`poll`
    calls, which is what makes the stream gap-free.
    """

    def __init__(
        self,
        source_mask: int = DEFAULT_SOURCE_MASK,
        hosts: Iterable[str] = DEFAULT_HOSTS,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.source_mask = source_mask
        self.hosts = tuple(hosts)
        if not self.hosts:
            raise ValueError("at least one host is required")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.cursor = 0
        self.copyright: str | None = None
        self._host_index = 0
        self._errors = 0

    @property
    def host(self) -> str:
        return self.hosts[self._host_index % len(self.hosts)]

    def _next_host(self) -> None:
        self._host_index += 1
        self.cursor = 0  # cursors are per-host; a new host must re-prime

    def poll(self) -> PollResult:
        """Fetch everything appended since the last cursor.

        The first call (cursor 0) primes the stream and returns no strokes --
        that is the server's behaviour, not an error.
        """
        url = f"https://{self.host}/l/"
        params = {"v": PROTOCOL_VERSION, "l": self.cursor, "i": self.source_mask}
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self._errors += 1
            if self._errors % 3 == 0:
                LOG.warning("switching away from %s after %d errors", self.host, self._errors)
                self._next_host()
            raise LightningMapsError(f"poll failed against {self.host}: {exc}") from exc

        self._errors = 0
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> PollResult:
        if "copyright" in data:
            self.copyright = data["copyright"]

        new_cursor = data.get("s")
        if isinstance(new_cursor, int):
            self.cursor = new_cursor

        server_time = data.get("t")
        strokes: list[Stroke] = []
        if server_time is not None:
            for raw in data.get("d") or []:
                stroke = self._parse_stroke(raw, float(server_time))
                if stroke is not None:
                    strokes.append(stroke)

        # ``w`` is the hint for a client with strokes in view, ``o`` for one
        # without. A headless collector wants the stream regardless, so take
        # whichever is shorter and honour it as the poll cadence.
        hints = [v for v in (data.get("w"), data.get("o")) if isinstance(v, (int, float)) and v > 0]
        wait_s = min(hints) / 1000.0 if hints else 1.0
        wait_s = max(MIN_WAIT_S, min(MAX_WAIT_S, wait_s))

        return PollResult(
            strokes=strokes,
            wait_s=wait_s,
            server_time=float(server_time) if server_time is not None else None,
            cursor=self.cursor,
            raw_keys=tuple(sorted(data.keys())),
        )

    @staticmethod
    def _parse_stroke(raw: Any, server_time: float) -> Stroke | None:
        if not isinstance(raw, dict):
            return None
        try:
            return Stroke(
                src=int(raw["src"]),
                stroke_id=int(raw["id"]),
                time_utc=server_time + float(raw["time"]) / 1000.0,
                lat=float(raw["lat"]),
                lon=float(raw["lon"]),
                dev_m=_opt_float(raw.get("dev")),
                delay_ms=_opt_int(raw.get("del")),
                alt_m=_opt_float(raw.get("alt")),
                server=_opt_int(raw.get("srv")),
            )
        except (KeyError, TypeError, ValueError):
            LOG.debug("skipping unparseable stroke: %r", raw)
            return None


def _opt_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
