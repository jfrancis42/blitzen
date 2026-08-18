"""Lightning-triggered protective shutdown.

State machine: cut power to protected equipment when lightning is detected
inside the trigger radius, restore it once the sky has been quiet for the
all-clear period.

    UNKNOWN --(feed healthy, no recent strikes)--> CLEAR      equipment ON
    CLEAR   --(strike inside trigger radius)-----> DANGER     equipment OFF
    DANGER  --(quiet for all_clear_minutes)------> CLEAR      equipment ON

Two safety rules sit on top of that:

* **Start closed.** On startup the state is UNKNOWN and the equipment is held
  OFF until the feed is confirmed live and the store shows no recent strikes.
  Power is never restored on an assumption.
* **Blind means unsafe.** If the feed goes stale, we cannot see lightning, so
  the equipment is held OFF regardless of state (``fail_safe_on_stale``).
  Silently leaving gear energised while blind is the one failure mode that
  defeats the whole purpose of the device.
"""

from __future__ import annotations

import logging
import sys
import time
from enum import Enum
from typing import Callable, TextIO

from .collector import NearbyStroke
from .config import Config
from .geo import KM_PER_MILE, km_to_miles
from .store import Store

LOG = logging.getLogger(__name__)


class State(Enum):
    UNKNOWN = "unknown"
    CLEAR = "clear"
    DANGER = "danger"


class Equipment:
    """Whatever actually switches the protected gear.

    Version 1 only prints. A relay-backed implementation slots in here without
    touching the state machine -- see :class:`PrintEquipment` for the contract.
    Both methods must be idempotent; the controller only calls them on an
    actual change, but a real driver should tolerate a repeat.
    """

    def power_on(self, reason: str) -> None:
        raise NotImplementedError

    def power_off(self, reason: str) -> None:
        raise NotImplementedError


class PrintEquipment(Equipment):
    """Version 1: announce what a real switch would have done."""

    def __init__(self, stream: TextIO | None = None, name: str = "EQUIPMENT") -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.name = name
        self.history: list[tuple[float, bool, str]] = []

    def _banner(self, powered: bool, reason: str) -> None:
        self.history.append((time.time(), powered, reason))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        action = "POWER ON " if powered else "POWER OFF"
        bar = "=" * 68
        print(
            f"\n{bar}\n"
            f"  {stamp}   {self.name}: {action}\n"
            f"  {reason}\n"
            f"{bar}",
            file=self.stream,
            flush=True,
        )

    def power_on(self, reason: str) -> None:
        self._banner(True, reason)

    def power_off(self, reason: str) -> None:
        self._banner(False, reason)


def effective_distance_km(stroke: NearbyStroke, use_margin: bool) -> float:
    """Distance to the near edge of the stroke's uncertainty circle.

    The network's own position uncertainty (``dev``) routinely runs 2-6 km,
    which is large next to a 10 mile (16 km) trigger radius. With the margin
    enabled, a stroke reported at 11 miles with 4 km of uncertainty is treated
    as being at roughly 9.8 miles -- i.e. we trip on the possibility, not on
    the point estimate. The map itself draws this circle at ``dev / 2``.
    """
    if not use_margin or stroke.stroke.dev_m is None:
        return stroke.distance_km
    return max(0.0, stroke.distance_km - (stroke.stroke.dev_m / 2.0) / 1000.0)


class ProtectionController:
    """Decides whether the protected equipment should be energised."""

    def __init__(
        self,
        config: Config,
        equipment: Equipment,
        store: Store | None = None,
        clock: Callable[[], float] = time.time,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.equipment = equipment
        self.store = store
        self.clock = clock
        self.announce = announce or (lambda msg: print(msg, flush=True))

        self.trigger_km = config.trigger_distance_miles * KM_PER_MILE
        self.all_clear_s = config.all_clear_minutes * 60.0

        self.state = State.UNKNOWN
        self.stale = True          # blind until a poll succeeds
        self.powered: bool | None = None
        self.last_trigger_utc: float | None = None
        self.last_trigger: NearbyStroke | None = None
        self.trigger_count = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Assert the safe default, then recover state from the database.

        Called before the feed is running, so the equipment goes OFF first and
        only comes back once we can see the sky again.
        """
        self._apply("startup: state unknown until the lightning feed is confirmed live")
        recent = self._recent_trigger_from_store()
        if recent is not None:
            age_min = (self.clock() - recent) / 60.0
            self.last_trigger_utc = recent
            self.state = State.DANGER
            self.announce(
                f"  recovered from database: strike within "
                f"{self.config.trigger_distance_miles:g} mi {age_min:.1f} min ago; "
                f"holding OFF for another {max(0.0, self.all_clear_s / 60.0 - age_min):.1f} min"
            )

    def _recent_trigger_from_store(self) -> float | None:
        """Most recent in-radius strike still inside the all-clear window."""
        if self.store is None:
            return None
        since = self.clock() - self.all_clear_s
        latest = None
        for row in self.store.strokes_since(since):
            distance = row["distance_km"]
            if self.config.use_uncertainty_margin and row["dev_m"] is not None:
                distance = max(0.0, distance - (row["dev_m"] / 2.0) / 1000.0)
            if distance <= self.trigger_km:
                stroke_utc = row["time_ms"] / 1000.0
                if latest is None or stroke_utc > latest:
                    latest = stroke_utc
        return latest

    # -- inputs ------------------------------------------------------------

    def note_stroke(self, nearby: NearbyStroke) -> None:
        """Feed one in-collection-radius stroke to the state machine."""
        distance_km = effective_distance_km(nearby, self.config.use_uncertainty_margin)
        if distance_km > self.trigger_km:
            return

        self.trigger_count += 1
        stroke_utc = nearby.stroke.time_utc
        if self.last_trigger_utc is None or stroke_utc > self.last_trigger_utc:
            self.last_trigger_utc = stroke_utc
            self.last_trigger = nearby

        miles = km_to_miles(distance_km)
        was_clear = self.state is not State.DANGER
        self.state = State.DANGER
        if was_clear:
            self._apply(
                f"lightning {miles:.1f} mi {nearby.compass} "
                f"(inside the {self.config.trigger_distance_miles:g} mi trigger radius)"
            )
        else:
            self.announce(
                f"  strike {miles:4.1f} mi {nearby.compass:<3} - all-clear timer reset "
                f"({self.config.all_clear_minutes:g} min)"
            )

    def tick(self, now: float, last_success_utc: float | None) -> None:
        """Re-evaluate on every poll, whether or not strokes arrived."""
        self._update_stale(now, last_success_utc)

        if self.state is State.DANGER and self.last_trigger_utc is not None:
            quiet_s = now - self.last_trigger_utc
            if quiet_s >= self.all_clear_s:
                self.state = State.CLEAR
                self._apply(
                    f"all clear: no lightning within "
                    f"{self.config.trigger_distance_miles:g} mi for "
                    f"{quiet_s / 60.0:.1f} min"
                )
                return

        if self.state is State.UNKNOWN and not self.stale:
            self.state = State.CLEAR
            self._apply(
                f"feed live, no lightning within "
                f"{self.config.trigger_distance_miles:g} mi"
            )
            return

        self._apply(None)

    def _update_stale(self, now: float, last_success_utc: float | None) -> None:
        if not self.config.fail_safe_on_stale:
            self.stale = False
            return
        age = None if last_success_utc is None else now - last_success_utc
        stale_now = age is None or age > self.config.stale_feed_seconds
        if stale_now and not self.stale:
            self.stale = True
            self._apply(
                f"lightning feed stale ({age:.0f}s without data) - "
                "holding equipment OFF while blind"
                if age is not None else "lightning feed unavailable - holding equipment OFF"
            )
        elif not stale_now and self.stale:
            self.stale = False
            self.announce("  lightning feed recovered")
            self._apply(None)

    # -- output ------------------------------------------------------------

    @property
    def should_be_powered(self) -> bool:
        return self.state is State.CLEAR and not self.stale

    def _apply(self, reason: str | None) -> None:
        """Drive the equipment if the desired power state changed."""
        desired = self.should_be_powered
        if desired == self.powered:
            return
        self.powered = desired
        text = reason or ("clear" if desired else "unsafe")
        if desired:
            self.equipment.power_on(text)
        else:
            self.equipment.power_off(text)

    def status_line(self, now: float | None = None) -> str:
        now = now if now is not None else self.clock()
        power = "ON" if self.powered else "OFF"
        bits = [f"state={self.state.value}", f"equipment={power}"]
        if self.stale:
            bits.append("FEED-STALE")
        if self.state is State.DANGER and self.last_trigger_utc is not None:
            remaining = max(0.0, self.all_clear_s - (now - self.last_trigger_utc))
            bits.append(f"all-clear in {remaining / 60.0:.1f} min")
            if self.last_trigger is not None:
                distance = effective_distance_km(
                    self.last_trigger, self.config.use_uncertainty_margin
                )
                bits.append(
                    f"last strike {km_to_miles(distance):.1f} mi "
                    f"{self.last_trigger.compass}"
                )
        bits.append(f"strikes in radius: {self.trigger_count}")
        return "  " + ", ".join(bits)
