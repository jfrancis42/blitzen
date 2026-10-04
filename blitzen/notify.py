"""Lightning alerts over the Signal gateway.

``blitzen alert`` runs the same protection state machine as ``protect`` but,
instead of switching equipment, sends a Signal message when the state changes
in a way a person cares about:

* **LIGHTNING**: the first strike inside the trigger radius. One message per
  storm, not one per strike -- a storm delivers hundreds.
* **ALL CLEAR**: ``all_clear_minutes`` with nothing inside the radius.
* **BLIND**: the feed has been down for ``blind_alert_minutes``. For an alert
  system this is the dangerous failure: silence reads as "no lightning", so
  silence while blind has to be announced.
* **FEED BACK**: sent only if a BLIND message went out.

Why this watches *state* rather than implementing ``Equipment``: the
controller only calls ``power_off`` when the desired power state changes. A
strike that arrives while power is already held off -- during a feed outage,
or in the first second after startup -- moves the state to DANGER without any
``power_off`` call, so an Equipment-based notifier would miss exactly the
alert that matters. Comparing ``controller.state`` before and after each input
cannot miss a transition.

Startup is silent. A restart in the middle of a storm recovers DANGER from the
database (see ``ProtectionController.start``); that storm already produced its
alert before the restart, so it is not repeated -- but its all-clear still is.

Sending happens on a worker thread. The feed must be read every ~500 ms and
the gateway can take seconds (or be down), so a blocking send would stall
collection -- and a stalled collector is a blind one.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .collector import NearbyStroke
from .geo import km_to_miles
from .protect import ProtectionController, State, effective_distance_km

LOG = logging.getLogger(__name__)

#: Retry schedule for one message, in seconds between attempts. A lightning
#: alert that arrives 20 minutes late is still worth more than none; the
#: gateway restarting (it is a JVM) takes about a minute.
RETRY_DELAYS_S = (5, 15, 30, 60, 120, 300, 600)


class SignalSender:
    """Posts messages to signal-cli-api's ``/send``, retrying in the background."""

    def __init__(
        self,
        url: str,
        token: str,
        recipients: list[str],
        retry_delays: tuple[float, ...] = RETRY_DELAYS_S,
        post: Callable[..., requests.Response] = requests.post,
    ) -> None:
        if not recipients:
            raise ValueError("no Signal recipients configured")
        self.url = url.rstrip("/") + "/send"
        self.token = token
        self.recipients = recipients
        self.retry_delays = retry_delays
        self._post = post
        self.delivered = 0
        self.failed = 0
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="signal-sender", daemon=True)
        self._thread.start()

    @classmethod
    def from_env(cls) -> "SignalSender":
        """Build from ``BLITZEN_SIGNAL_*`` environment variables.

        Deployment settings, not lightning settings, so they are kept out of
        config.json: the same config runs on a laptop with no gateway at all.
        """
        url = os.environ.get("BLITZEN_SIGNAL_URL", "http://127.0.0.1:8085")
        token = os.environ.get("BLITZEN_SIGNAL_TOKEN", "").strip()
        if not token:
            token_file = os.environ.get("BLITZEN_SIGNAL_TOKEN_FILE")
            if not token_file:
                raise ValueError(
                    "set BLITZEN_SIGNAL_TOKEN or BLITZEN_SIGNAL_TOKEN_FILE"
                )
            token = Path(token_file).read_text(encoding="utf-8").strip()
        recipients = [
            r.strip() for r in os.environ.get("BLITZEN_SIGNAL_TO", "").split(",") if r.strip()
        ]
        return cls(url, token, recipients)

    def send(self, text: str) -> None:
        """Queue a message; never blocks and never raises."""
        LOG.info("queueing alert: %s", text.replace("\n", " | "))
        self._queue.put(text)

    def close(self, timeout: float = 10.0) -> None:
        """Give queued messages a short chance to go out on shutdown."""
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            if self._deliver(text):
                self.delivered += 1
            else:
                self.failed += 1

    def _deliver(self, text: str) -> bool:
        for attempt, delay in enumerate((0.0, *self.retry_delays)):
            if delay:
                time.sleep(delay)
            try:
                resp = self._post(
                    self.url,
                    json={"to": self.recipients, "message": text},
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=60,
                )
                if resp.status_code < 400:
                    print(f"  signal: sent ({len(text)} chars)", flush=True)
                    return True
                # 401 will not fix itself; say so plainly rather than retrying
                # for twenty minutes and looking like a flaky network.
                if resp.status_code in (401, 403):
                    print(
                        f"  signal: REFUSED HTTP {resp.status_code} -- the blitzen "
                        "token is missing from the gateway's token file",
                        flush=True,
                    )
                    return False
                err = f"HTTP {resp.status_code} {resp.text[:200]}"
            except requests.RequestException as exc:
                err = str(exc)
            print(f"  signal: attempt {attempt + 1} failed: {err}", flush=True)
        print(f"  signal: GAVE UP on message: {text!r}", flush=True)
        return False


def _clock(utc: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(utc))


class LightningAlerter:
    """Turns protection-controller state changes into human messages."""

    def __init__(
        self,
        controller: ProtectionController,
        send: Callable[[str], None],
        place: str = "home",
    ) -> None:
        self.controller = controller
        self.config = controller.config
        self.send = send
        self.place = place
        self.blind_alert_s = self.config.blind_alert_minutes * 60.0

        self._storm_start_count = 0
        self._nearest_km: float | None = None
        self._stale_since: float | None = None
        self._blind_announced = False

    # -- inputs, wired to the Collector in place of the controller's own -----

    def on_stroke(self, nearby: NearbyStroke) -> None:
        before = self.controller.state
        self.controller.note_stroke(nearby)
        if self.controller.state is not State.DANGER:
            return
        distance = effective_distance_km(nearby, self.config.use_uncertainty_margin)
        if distance > self.controller.trigger_km:
            return
        if before is not State.DANGER:
            self._storm_start_count = self.controller.trigger_count - 1
            self._nearest_km = nearby.distance_km
            self.send(self._lightning_text(nearby))
        elif self._nearest_km is None or nearby.distance_km < self._nearest_km:
            self._nearest_km = nearby.distance_km

    def on_tick(self, now: float, last_success_utc: float | None) -> None:
        before = self.controller.state
        self.controller.tick(now, last_success_utc)
        after = self.controller.state

        if before is State.DANGER and after is State.CLEAR:
            self.send(self._all_clear_text(now))
            self._nearest_km = None

        self._track_blindness(now, last_success_utc)

    # -- blindness -----------------------------------------------------------

    def _track_blindness(self, now: float, last_success_utc: float | None) -> None:
        if self.controller.stale:
            if self._stale_since is None:
                self._stale_since = last_success_utc or now
            if not self._blind_announced and now - self._stale_since >= self.blind_alert_s:
                self._blind_announced = True
                self.send(
                    f"⚠️ blitzen is BLIND: no lightning data since "
                    f"{_clock(self._stale_since)}. No lightning alerts can be "
                    f"sent until the feed recovers -- silence does not mean "
                    f"the sky is clear."
                )
            return

        if self._blind_announced:
            minutes = (now - (self._stale_since or now)) / 60.0
            self.send(
                f"blitzen lightning feed is back after {minutes:.0f} min. "
                f"Alerts resumed. {self._state_sentence()}"
            )
        self._stale_since = None
        self._blind_announced = False

    # -- message text --------------------------------------------------------

    def _lightning_text(self, nearby: NearbyStroke) -> str:
        miles = km_to_miles(nearby.distance_km)
        text = (
            f"⚡ LIGHTNING {miles:.1f} mi {nearby.compass} of {self.place} "
            f"at {_clock(nearby.stroke.time_utc)}."
        )
        dev_m = nearby.stroke.dev_m
        if dev_m:
            near_edge = km_to_miles(max(0.0, nearby.distance_km - dev_m / 2000.0))
            text += f" Position uncertain by ±{dev_m / 2000.0:.1f} km"
            if self.config.use_uncertainty_margin and miles > self.config.trigger_distance_miles:
                text += f" -- could be as close as {near_edge:.1f} mi"
            text += "."
        text += (
            f"\nAll-clear follows after {self.config.all_clear_minutes:g} min "
            f"with no strike inside {self.config.trigger_distance_miles:g} mi."
        )
        return text

    def _all_clear_text(self, now: float) -> str:
        strikes = self.controller.trigger_count - self._storm_start_count
        text = (
            f"✅ All clear: no lightning within "
            f"{self.config.trigger_distance_miles:g} mi of {self.place} for "
            f"{self.config.all_clear_minutes:g} min."
        )
        if self.controller.last_trigger_utc is not None:
            text += f" Last strike {_clock(self.controller.last_trigger_utc)}"
            if self._nearest_km is not None:
                text += f"; closest {km_to_miles(self._nearest_km):.1f} mi"
            if strikes > 0:
                text += f"; {strikes} strike(s) inside the radius"
            text += "."
        if self.controller.stale:
            # The timer ran out while we could not see. That is not a
            # confirmed all-clear and must not read like one.
            text += (
                "\n⚠️ BUT the lightning feed is currently DOWN, so this is "
                "only the timer expiring, not a confirmed clear sky."
            )
        return text

    def _state_sentence(self) -> str:
        if self.controller.state is State.DANGER:
            return (
                f"Lightning alert still active (a strike within "
                f"{self.config.trigger_distance_miles:g} mi in the last "
                f"{self.config.all_clear_minutes:g} min)."
            )
        return f"Nothing within {self.config.trigger_distance_miles:g} mi."
