"""Offline tests -- no network access required.

Run with:  python3 -m pytest tests/ -q      (from the project root)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blitzen.collector import Collector, NearbyStroke
from blitzen.config import Config
from blitzen.geo import compass_point, haversine_km, initial_bearing_deg, km_to_miles
from blitzen.protect import (
    Equipment,
    PrintEquipment,
    ProtectionController,
    State,
    effective_distance_km,
)
from blitzen.source import LightningMapsSource, PollResult, Stroke
from blitzen.store import Store

HOME_LAT, HOME_LON = 39.525445, -104.767522

# A real response body captured from live.lightningmaps.org on 2026-08-17.
SAMPLE_RESPONSE = {
    "w": 500,
    "o": 500,
    "s": 2405016,
    "t": 1787012480,
    "d": [
        {"time": -13145, "lat": "42.070180", "lon": "-99.145679",
         "src": 2, "srv": 1, "id": 2891368, "del": 1798, "dev": 705},
        {"time": -4845, "lat": "39.600000", "lon": "-104.800000",
         "src": 1, "srv": 1, "id": 2891605, "del": 1851, "dev": 6591},
    ],
}


def test_haversine_known_distance():
    # Denver (DEN) to Colorado Springs (COS): ~110 km.
    d = haversine_km(39.8617, -104.6731, 38.8058, -104.7008)
    assert 110 < d < 118


def test_haversine_zero_and_symmetry():
    assert haversine_km(HOME_LAT, HOME_LON, HOME_LAT, HOME_LON) == pytest.approx(0.0)
    a = haversine_km(39.0, -104.0, 40.0, -105.0)
    b = haversine_km(40.0, -105.0, 39.0, -104.0)
    assert a == pytest.approx(b)


def test_bearing_and_compass():
    assert initial_bearing_deg(0.0, 0.0, 10.0, 0.0) == pytest.approx(0.0, abs=0.01)
    assert initial_bearing_deg(0.0, 0.0, 0.0, 10.0) == pytest.approx(90.0, abs=0.01)
    assert compass_point(0) == "N"
    assert compass_point(90) == "E"
    assert compass_point(181) == "S"
    assert compass_point(359) == "N"


def test_km_to_miles():
    assert km_to_miles(1.609344) == pytest.approx(1.0)


def test_parse_response_time_and_units():
    """Absolute time is ``t + time/1000`` -- the formula realtime.js uses."""
    source = LightningMapsSource()
    result = source._parse(SAMPLE_RESPONSE)

    assert isinstance(result, PollResult)
    assert len(result.strokes) == 2
    assert source.cursor == 2405016
    assert result.wait_s == 0.5

    first = result.strokes[0]
    assert first.time_utc == pytest.approx(1787012480 - 13.145)
    assert first.lat == pytest.approx(42.070180)
    assert first.lon == pytest.approx(-99.145679)
    assert first.dev_m == 705
    assert first.delay_ms == 1798
    assert first.source_name == "lightningmaps"
    assert result.strokes[1].source_name == "blitzortung"


def test_parse_priming_response_has_no_strokes():
    """A cursor-0 response carries no ``t`` and no ``d``; that is not an error."""
    source = LightningMapsSource()
    result = source._parse({"w": 500, "o": 500, "s": 999, "x": True, "copyright": "..."})
    assert result.strokes == []
    assert source.cursor == 999
    assert source.copyright == "..."


def test_parse_skips_malformed_strokes():
    source = LightningMapsSource()
    result = source._parse({
        "s": 1, "t": 1787012480,
        "d": [
            {"time": 0, "lat": "39.5", "lon": "-104.7", "src": 1, "id": 1},
            {"time": 0, "lat": "bogus", "lon": "-104.7", "src": 1, "id": 2},
            {"missing": "everything"},
            None,
        ],
    })
    assert len(result.strokes) == 1
    assert result.strokes[0].stroke_id == 1


def test_wait_hint_is_clamped():
    source = LightningMapsSource()
    assert source._parse({"s": 1, "w": 1, "o": 1}).wait_s == 0.5          # floor
    assert source._parse({"s": 1, "w": 999999, "o": 999999}).wait_s == 60.0  # ceiling
    assert source._parse({"s": 1}).wait_s == 1.0                          # no hint


def _stroke(lat, lon, stroke_id=1, when=None):
    return Stroke(src=1, stroke_id=stroke_id, time_utc=when or time.time(), lat=lat, lon=lon)


def test_radius_filter(tmp_path):
    config = Config(lat=HOME_LAT, lon=HOME_LON, radius_km=100.0,
                    database=str(tmp_path / "t.db"))
    collector = Collector(config, store=Store(tmp_path / "t.db"))

    strokes = [
        _stroke(39.6, -104.8, 1),      # ~9 km  -> in
        _stroke(38.8058, -104.7008, 2),  # ~80 km -> in
        _stroke(41.0, -101.7, 3),      # ~450 km -> out
        _stroke(-33.0, 151.0, 4),      # Sydney -> out
    ]
    nearby = collector.filter_nearby(strokes)
    assert {n.stroke.stroke_id for n in nearby} == {1, 2}
    assert all(n.distance_km <= 100.0 for n in nearby)
    collector.store.close()


def test_store_roundtrip_and_dedup(tmp_path):
    with Store(tmp_path / "s.db") as store:
        s = _stroke(39.6, -104.8, 42, when=1787012480.5)
        row = Store.row_for(s, 9.1, 305.0)

        assert store.add_strokes([row]) == 1
        assert store.add_strokes([row]) == 0, "re-delivered stroke must not duplicate"
        assert store.total() == 1

        got = store.strokes_since(1787012000)
        assert len(got) == 1
        assert got[0]["time_ms"] == 1787012480500
        assert got[0]["distance_km"] == pytest.approx(9.1)

        assert store.strokes_since(1787013000) == []
        assert store.nearest_since(1787012000)["stroke_id"] == 42


def test_store_dedup_distinguishes_wrapped_ids(tmp_path):
    """Ids wrap, so the same id at a different time is a different stroke."""
    with Store(tmp_path / "w.db") as store:
        a = Store.row_for(_stroke(39.6, -104.8, 7, when=1787012480.0), 9.0, 300.0)
        b = Store.row_for(_stroke(39.7, -104.9, 7, when=1787019999.0), 9.0, 300.0)
        assert store.add_strokes([a, b]) == 2


def test_store_prune(tmp_path):
    with Store(tmp_path / "p.db") as store:
        old = Store.row_for(_stroke(39.6, -104.8, 1, when=1000.0), 9.0, 300.0)
        new = Store.row_for(_stroke(39.6, -104.8, 2, when=1787012480.0), 9.0, 300.0)
        store.add_strokes([old, new])
        assert store.prune(500000.0) == 1
        assert store.total() == 1


def test_config_defaults_and_validation():
    config = Config()
    assert config.lat == pytest.approx(HOME_LAT)
    assert config.lon == pytest.approx(HOME_LON)
    assert config.interval_minutes == 5.0
    config.validate()

    for bad in (Config(lat=91.0), Config(lon=-181.0), Config(radius_km=0),
                Config(interval_minutes=0), Config(poll_seconds=0), Config(source_mask=0)):
        with pytest.raises(ValueError):
            bad.validate()


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"lat": 1.0, "wat": 2}')
    with pytest.raises(ValueError, match="wat"):
        Config.load(path)


def test_collector_reports_without_network(tmp_path):
    """Drive the loop with a stubbed source: no sockets, deterministic result."""

    class StubSource:
        cursor = 1
        host = "stub"

        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            return PollResult(
                strokes=[_stroke(39.6, -104.8, self.calls), _stroke(-33.0, 151.0, 999)],
                wait_s=0.5,
            )

    config = Config(radius_km=100.0, interval_minutes=1 / 60, retain_days=0,
                    database=str(tmp_path / "c.db"))
    with Store(tmp_path / "c.db") as store:
        collector = Collector(config, store=store, source=StubSource())
        reports = collector.run(max_intervals=1)

    assert len(reports) == 1
    report = reports[0]
    assert report.polls >= 1
    assert report.strokes_seen == report.polls * 2
    assert report.strokes_nearby == report.polls      # Sydney filtered out
    assert report.nearest is not None
    assert report.nearest.distance_km < 100
    assert "within 100 km" in report.summary(config.radius_km)


# --------------------------------------------------------------------------
# Protective shutdown
# --------------------------------------------------------------------------

MILE_KM = 1.609344


class FakeEquipment(Equipment):
    """Records commands instead of switching anything."""

    def __init__(self):
        self.calls = []

    def power_on(self, reason):
        self.calls.append(("on", reason))

    def power_off(self, reason):
        self.calls.append(("off", reason))

    @property
    def powered(self):
        return self.calls[-1][0] == "on" if self.calls else None


class FakeClock:
    def __init__(self, start=1787012480.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance_minutes(self, minutes):
        self.now += minutes * 60.0


def _controller(clock=None, store=None, **overrides):
    config = Config(radius_km=100.0, **overrides)
    equipment = FakeEquipment()
    clock = clock or FakeClock()
    controller = ProtectionController(
        config, equipment, store=store, clock=clock, announce=lambda _msg: None
    )
    return controller, equipment, clock


def _nearby(miles, when, dev_m=None, bearing=0.0):
    km = miles * MILE_KM
    stroke = Stroke(src=1, stroke_id=1, time_utc=when, lat=HOME_LAT + 0.1,
                    lon=HOME_LON, dev_m=dev_m)
    return NearbyStroke(stroke, km, bearing)


def test_starts_powered_off_until_feed_confirmed():
    """Power must never be assumed safe at startup."""
    controller, equipment, clock = _controller()
    controller.start()
    assert controller.state is State.UNKNOWN
    assert equipment.powered is False, "equipment must start OFF"

    controller.tick(clock.now, clock.now)
    assert controller.state is State.CLEAR
    assert equipment.powered is True


def test_strike_inside_radius_cuts_power():
    controller, equipment, clock = _controller()
    controller.start()
    controller.tick(clock.now, clock.now)
    assert equipment.powered is True

    controller.note_stroke(_nearby(7.0, clock.now))
    assert controller.state is State.DANGER
    assert equipment.powered is False
    assert "7.0 mi" in equipment.calls[-1][1]


def test_strike_outside_radius_is_ignored():
    controller, equipment, clock = _controller()
    controller.start()
    controller.tick(clock.now, clock.now)

    controller.note_stroke(_nearby(12.0, clock.now))
    assert controller.state is State.CLEAR
    assert equipment.powered is True


def test_power_restored_after_all_clear():
    controller, equipment, clock = _controller()
    controller.start()
    controller.tick(clock.now, clock.now)
    controller.note_stroke(_nearby(3.0, clock.now))
    assert equipment.powered is False

    clock.advance_minutes(29.0)
    controller.tick(clock.now, clock.now)
    assert equipment.powered is False, "must stay off inside the all-clear window"

    clock.advance_minutes(1.5)
    controller.tick(clock.now, clock.now)
    assert controller.state is State.CLEAR
    assert equipment.powered is True
    assert "30" in equipment.calls[-1][1] or "31" in equipment.calls[-1][1]


def test_second_strike_resets_all_clear_timer():
    controller, equipment, clock = _controller()
    controller.start()
    controller.tick(clock.now, clock.now)
    controller.note_stroke(_nearby(5.0, clock.now))

    clock.advance_minutes(25.0)
    controller.note_stroke(_nearby(5.0, clock.now))   # resets
    clock.advance_minutes(10.0)                        # 35 min from first strike
    controller.tick(clock.now, clock.now)
    assert equipment.powered is False, "timer must run from the latest strike"

    clock.advance_minutes(21.0)
    controller.tick(clock.now, clock.now)
    assert equipment.powered is True


def test_stale_feed_holds_power_off():
    controller, equipment, clock = _controller(stale_feed_seconds=120.0)
    controller.start()
    controller.tick(clock.now, clock.now)
    assert equipment.powered is True

    clock.advance_minutes(5.0)
    controller.tick(clock.now, clock.now - 300)   # last good poll 5 min ago
    assert equipment.powered is False
    assert "stale" in equipment.calls[-1][1].lower()

    controller.tick(clock.now, clock.now)         # feed recovers
    assert equipment.powered is True


def test_fail_safe_can_be_disabled():
    controller, equipment, clock = _controller(fail_safe_on_stale=False)
    controller.start()
    controller.tick(clock.now, None)
    assert equipment.powered is True, "without fail-safe, a blind feed leaves power on"


def test_uncertainty_margin_trips_on_near_edge():
    """A stroke reported at 11 mi with 4 km of error may really be at 9.8 mi."""
    outside = _nearby(11.0, 0.0, dev_m=4000)
    assert effective_distance_km(outside, use_margin=False) == pytest.approx(11 * MILE_KM)
    assert effective_distance_km(outside, use_margin=True) == pytest.approx(
        11 * MILE_KM - 2.0
    )

    controller, equipment, clock = _controller(use_uncertainty_margin=True)
    controller.start()
    controller.tick(clock.now, clock.now)
    controller.note_stroke(_nearby(11.0, clock.now, dev_m=4000))
    assert equipment.powered is False, "margin should trip on an 11 mi report"

    strict, strict_eq, strict_clock = _controller(use_uncertainty_margin=False)
    strict.start()
    strict.tick(strict_clock.now, strict_clock.now)
    strict.note_stroke(_nearby(11.0, strict_clock.now, dev_m=4000))
    assert strict_eq.powered is True, "without the margin, 11 mi is outside 10 mi"


def test_recovers_danger_state_from_database(tmp_path):
    """A restart mid-storm must not re-energise the gear."""
    clock = FakeClock()
    with Store(tmp_path / "r.db") as store:
        stroke = Stroke(src=1, stroke_id=5, time_utc=clock.now - 300,
                        lat=HOME_LAT + 0.05, lon=HOME_LON)
        store.add_strokes([Store.row_for(stroke, 5.0 * MILE_KM, 0.0)])

        controller, equipment, _ = _controller(clock=clock, store=store)
        controller.start()
        assert controller.state is State.DANGER
        assert equipment.powered is False

        controller.tick(clock.now, clock.now)
        assert equipment.powered is False, "5 min into a 30 min window"

        clock.advance_minutes(26.0)
        controller.tick(clock.now, clock.now)
        assert equipment.powered is True


def test_old_database_strike_does_not_hold_power_off(tmp_path):
    clock = FakeClock()
    with Store(tmp_path / "o.db") as store:
        stroke = Stroke(src=1, stroke_id=5, time_utc=clock.now - 3600,
                        lat=HOME_LAT + 0.05, lon=HOME_LON)
        store.add_strokes([Store.row_for(stroke, 5.0 * MILE_KM, 0.0)])

        controller, equipment, _ = _controller(clock=clock, store=store)
        controller.start()
        assert controller.state is State.UNKNOWN
        controller.tick(clock.now, clock.now)
        assert equipment.powered is True


def test_equipment_only_commanded_on_change():
    controller, equipment, clock = _controller()
    controller.start()
    controller.tick(clock.now, clock.now)
    before = len(equipment.calls)
    for _ in range(20):
        clock.advance_minutes(0.1)
        controller.tick(clock.now, clock.now)
    assert len(equipment.calls) == before, "no redundant switching"


def test_trigger_radius_must_fit_inside_collection_radius():
    """A trigger wider than the collection radius could never fire."""
    with pytest.raises(ValueError, match="exceeds radius_km"):
        Config(radius_km=10.0, trigger_distance_miles=10.0).validate()
    Config(radius_km=100.0, trigger_distance_miles=10.0).validate()


def test_print_equipment_records_and_outputs(capsys):
    equipment = PrintEquipment(name="TEST RIG")
    equipment.power_off("lightning 2.0 mi N")
    out = capsys.readouterr().out
    assert "POWER OFF" in out
    assert "TEST RIG" in out
    assert "lightning 2.0 mi N" in out
    assert equipment.history[-1][1] is False


def test_collector_tick_fires_without_strokes(tmp_path):
    """The all-clear timer depends on ticks arriving when nothing is happening."""
    ticks = []

    class QuietSource:
        cursor = 1
        host = "stub"

        def poll(self):
            return PollResult(strokes=[], wait_s=0.5)

    config = Config(interval_minutes=1 / 60, retain_days=0,
                    database=str(tmp_path / "t.db"))
    with Store(tmp_path / "t.db") as store:
        collector = Collector(config, store=store, source=QuietSource(),
                              on_tick=lambda now, last: ticks.append((now, last)))
        collector.run(max_intervals=1)

    assert ticks, "on_tick must fire even when no strokes arrive"
    assert all(last is not None for _now, last in ticks)


def test_collector_tick_survives_consumer_exception(tmp_path):
    class QuietSource:
        cursor = 1
        host = "stub"

        def poll(self):
            return PollResult(strokes=[], wait_s=0.5)

    def boom(_now, _last):
        raise RuntimeError("consumer blew up")

    config = Config(interval_minutes=1 / 60, retain_days=0,
                    database=str(tmp_path / "b.db"))
    with Store(tmp_path / "b.db") as store:
        collector = Collector(config, store=store, source=QuietSource(), on_tick=boom)
        reports = collector.run(max_intervals=1)
    assert reports[0].polls >= 1, "collection must continue despite a bad consumer"


# -- Signal alerting (notify.py) ---------------------------------------------

from blitzen.notify import LightningAlerter, SignalSender  # noqa: E402


def _alerter(**overrides):
    controller, _equipment, clock = _controller(**overrides)
    sent = []
    alerter = LightningAlerter(controller, sent.append)
    controller.start()
    alerter.on_tick(clock.now, clock.now)
    return alerter, controller, clock, sent


def test_alert_once_per_storm_then_all_clear():
    alerter, controller, clock, sent = _alerter()
    assert sent == [], "startup must be silent"

    alerter.on_stroke(_nearby(6, clock.now))
    for _ in range(5):
        clock.advance_minutes(1)
        alerter.on_stroke(_nearby(4, clock.now))
        alerter.on_tick(clock.now, clock.now)
    assert len(sent) == 1, "one message per storm, not one per strike"
    assert sent[0].startswith("⚡ LIGHTNING 6.0 mi")

    clock.advance_minutes(29)
    alerter.on_tick(clock.now, clock.now)
    assert len(sent) == 1, "all-clear must wait the full 30 quiet minutes"
    clock.advance_minutes(1.1)
    alerter.on_tick(clock.now, clock.now)
    assert len(sent) == 2 and sent[1].startswith("✅ All clear")
    assert "closest 4.0 mi" in sent[1] and "6 strike(s)" in sent[1]

    clock.advance_minutes(5)
    alerter.on_stroke(_nearby(3, clock.now))
    assert len(sent) == 3 and "LIGHTNING" in sent[2], "a new storm alerts again"


def test_strike_outside_trigger_sends_nothing():
    alerter, _controller_, clock, sent = _alerter()
    alerter.on_stroke(_nearby(14, clock.now))
    alerter.on_tick(clock.now, clock.now)
    assert sent == []


def test_alert_fires_even_when_power_already_held_off():
    """A strike during a feed outage moves state without a power_off call.

    This is the reason the alerter watches state rather than Equipment.
    """
    alerter, controller, clock, sent = _alerter()
    last_good = clock.now
    clock.advance_minutes(3)
    alerter.on_tick(clock.now, last_good)          # stale -> power held off
    assert controller.stale
    alerter.on_stroke(_nearby(5, clock.now))
    assert any("LIGHTNING" in m for m in sent)


def test_blind_feed_is_announced_and_recovery_reported():
    alerter, _controller_, clock, sent = _alerter(blind_alert_minutes=10)
    last_good = clock.now
    clock.advance_minutes(5)
    alerter.on_tick(clock.now, last_good)
    assert sent == [], "a short outage is noise, not news"
    clock.advance_minutes(6)
    alerter.on_tick(clock.now, last_good)
    clock.advance_minutes(1)
    alerter.on_tick(clock.now, last_good)
    assert len(sent) == 1 and "BLIND" in sent[0]

    clock.advance_minutes(1)
    alerter.on_tick(clock.now, clock.now)
    assert len(sent) == 2 and "back" in sent[1]


def test_short_outage_sends_no_recovery_message():
    alerter, _controller_, clock, sent = _alerter(blind_alert_minutes=10)
    last_good = clock.now
    clock.advance_minutes(4)
    alerter.on_tick(clock.now, last_good)
    alerter.on_tick(clock.now, clock.now)
    assert sent == []


def test_all_clear_while_blind_says_so():
    alerter, _controller_, clock, sent = _alerter(blind_alert_minutes=60)
    alerter.on_stroke(_nearby(5, clock.now))
    last_good = clock.now
    clock.advance_minutes(31)
    alerter.on_tick(clock.now, last_good)
    assert "All clear" in sent[-1] and "DOWN" in sent[-1]


def test_restart_mid_storm_is_silent_but_still_clears(tmp_path):
    clock = FakeClock()
    with Store(tmp_path / "s.db") as store:
        nearby = _nearby(5, clock.now)
        store.add_strokes([Store.row_for(nearby.stroke, nearby.distance_km, 0.0)])
        clock.advance_minutes(2)

        c2, _e2, _c2 = _controller(clock=clock, store=store)
        sent = []
        alerter = LightningAlerter(c2, sent.append)
        c2.start()
        assert c2.state is State.DANGER
        alerter.on_tick(clock.now, clock.now)
        alerter.on_stroke(_nearby(4, clock.now))
        assert sent == [], "the storm was already announced before the restart"
        clock.advance_minutes(31)
        alerter.on_tick(clock.now, clock.now)
        assert len(sent) == 1 and "All clear" in sent[0]


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = ""


def test_sender_retries_then_delivers():
    codes = [503, 503, 200]
    calls = []

    def post(url, json, headers, timeout):
        calls.append((url, json, headers))
        return _Resp(codes.pop(0))

    sender = SignalSender("http://gw:8085/", "tok", ["+1555"], retry_delays=(0.01, 0.01), post=post)
    sender.send("hello")
    sender.close()
    assert sender.delivered == 1 and len(calls) == 3
    assert calls[0][0] == "http://gw:8085/send"
    assert calls[0][1] == {"to": ["+1555"], "message": "hello"}
    assert calls[0][2]["Authorization"] == "Bearer tok"


def test_sender_does_not_retry_an_auth_refusal():
    calls = []

    def post(*_a, **_k):
        calls.append(1)
        return _Resp(401)

    sender = SignalSender("http://gw", "bad", ["+1555"], retry_delays=(0.01,) * 5, post=post)
    sender.send("x")
    sender.close()
    assert sender.failed == 1 and len(calls) == 1
