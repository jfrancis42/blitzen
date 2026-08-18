"""Continuous collector: follow the live feed, keep what is near home."""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .geo import compass_point, haversine_km, initial_bearing_deg, km_to_miles
from .source import LightningMapsError, LightningMapsSource, Stroke
from .store import Store

LOG = logging.getLogger(__name__)

#: Back-off applied after a failed poll, in seconds, capped.
BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 120.0


@dataclass
class NearbyStroke:
    """A stroke that passed the radius filter, with home-relative geometry."""

    stroke: Stroke
    distance_km: float
    bearing_deg: float

    @property
    def compass(self) -> str:
        return compass_point(self.bearing_deg)

    def describe(self) -> str:
        s = self.stroke
        stamp = time.strftime("%H:%M:%S", time.gmtime(s.time_utc))
        return (
            f"{stamp}Z  {self.distance_km:6.1f} km "
            f"({km_to_miles(self.distance_km):5.1f} mi) {self.compass:<3} "
            f"{s.lat:9.4f},{s.lon:10.4f}  {s.source_name}"
        )


@dataclass
class IntervalReport:
    """What happened during one reporting interval."""

    start_utc: float
    end_utc: float
    polls: int = 0
    poll_errors: int = 0
    strokes_seen: int = 0        # everything the feed delivered, worldwide
    strokes_nearby: int = 0      # inside radius_km
    strokes_stored: int = 0      # actually new in the database
    nearest: NearbyStroke | None = None
    examples: list[NearbyStroke] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.end_utc - self.start_utc

    def summary(self, radius_km: float) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(self.end_utc))
        head = (
            f"[{stamp}] {self.duration_s / 60:.1f} min: "
            f"{self.strokes_nearby} within {radius_km:.0f} km "
            f"({self.strokes_stored} new), {self.strokes_seen} worldwide, "
            f"{self.polls} polls"
        )
        if self.poll_errors:
            head += f", {self.poll_errors} errors"
        if self.nearest is None:
            return head + "\n  no strokes in range"
        lines = [head, f"  nearest: {self.nearest.describe()}"]
        for item in self.examples[:5]:
            if item is not self.nearest:
                lines.append(f"           {item.describe()}")
        return "\n".join(lines)


class Collector:
    """Follows the cursor stream continuously, reports every N minutes.

    The two cadences are deliberately different. The feed is a live cursor
    stream that expects to be read every ~500 ms; the reporting interval is
    just how often results are summarised. Reading the feed only once per
    interval would silently drop nearly everything.
    """

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        source: LightningMapsSource | None = None,
        on_report: Callable[[IntervalReport], None] | None = None,
        on_stroke: Callable[[NearbyStroke], None] | None = None,
        on_tick: Callable[[float, float | None], None] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.store = store if store is not None else Store(config.resolved_database())
        self.source = source or LightningMapsSource(source_mask=config.source_mask)
        self.on_report = on_report
        self.on_stroke = on_stroke
        #: Called after every poll attempt, successful or not, as
        #: ``on_tick(now, last_success_utc)``. This is what lets a consumer act
        #: on the *absence* of lightning -- an all-clear timer never fires if it
        #: is only driven by arriving strokes.
        self.on_tick = on_tick
        self.last_success_utc: float | None = None
        self._stop = False
        self._last_prune = 0.0

    def request_stop(self, *_args) -> None:
        LOG.info("stop requested; finishing current interval")
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def filter_nearby(self, strokes: list[Stroke]) -> list[NearbyStroke]:
        lat, lon, radius = self.config.lat, self.config.lon, self.config.radius_km
        out = []
        for s in strokes:
            distance = haversine_km(lat, lon, s.lat, s.lon)
            if distance <= radius:
                out.append(NearbyStroke(s, distance, initial_bearing_deg(lat, lon, s.lat, s.lon)))
        return out

    def run(self, max_intervals: int | None = None) -> list[IntervalReport]:
        """Run until stopped, emitting a report every ``interval_minutes``."""
        interval_s = self.config.interval_minutes * 60.0
        reports: list[IntervalReport] = []
        backoff = BACKOFF_START_S

        if self.source.cursor == 0:
            self._prime()

        while not self._stop:
            report = IntervalReport(start_utc=time.time(), end_utc=time.time())
            deadline = report.start_utc + interval_s

            while not self._stop and time.time() < deadline:
                try:
                    result = self.source.poll()
                    self.last_success_utc = time.time()
                    backoff = BACKOFF_START_S
                except LightningMapsError as exc:
                    report.poll_errors += 1
                    LOG.warning("%s (retrying in %.0fs)", exc, backoff)
                    self._tick()
                    self._sleep(min(backoff, max(0.0, deadline - time.time())))
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
                    continue

                report.polls += 1
                report.strokes_seen += len(result.strokes)
                nearby = self.filter_nearby(result.strokes)
                if nearby:
                    self._record(nearby, report)

                # Ticked after the strokes are dispatched, so a consumer sees
                # this poll's lightning before it re-evaluates its timers.
                self._tick()

                wait = self.config.poll_seconds or result.wait_s
                self._sleep(min(wait, max(0.0, deadline - time.time())))

            report.end_utc = time.time()
            reports.append(report)
            if self.on_report:
                self.on_report(report)
            self._maybe_prune()

            if max_intervals is not None and len(reports) >= max_intervals:
                break

        return reports

    def _tick(self) -> None:
        """Notify the tick consumer, isolating the loop from its failures.

        A crash in a downstream consumer must not take out collection, but it
        must be loud -- a protection controller that has stopped being called
        is exactly the kind of silent failure this device cannot have.
        """
        if self.on_tick is None:
            return
        try:
            self.on_tick(time.time(), self.last_success_utc)
        except Exception:
            LOG.exception("on_tick consumer raised; collection continues")

    def _prime(self) -> None:
        """Establish the cursor. The priming poll returns no strokes by design."""
        try:
            result = self.source.poll()
            self.last_success_utc = time.time()
            LOG.info("primed cursor %s on %s", result.cursor, self.source.host)
        except LightningMapsError as exc:
            LOG.warning("priming poll failed: %s", exc)

    def _record(self, nearby: list[NearbyStroke], report: IntervalReport) -> None:
        rows = [Store.row_for(n.stroke, n.distance_km, n.bearing_deg) for n in nearby]
        stored = self.store.add_strokes(rows)
        report.strokes_nearby += len(nearby)
        report.strokes_stored += stored
        for item in nearby:
            if report.nearest is None or item.distance_km < report.nearest.distance_km:
                report.nearest = item
            if len(report.examples) < 20:
                report.examples.append(item)
            if self.on_stroke:
                self.on_stroke(item)

    def _maybe_prune(self) -> None:
        if self.config.retain_days <= 0:
            return
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        removed = self.store.prune(now - self.config.retain_days * 86400)
        if removed:
            LOG.info("pruned %d stroke(s) older than %.1f days", removed, self.config.retain_days)

    def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so a stop request is honoured promptly."""
        end = time.time() + seconds
        while not self._stop:
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25))
