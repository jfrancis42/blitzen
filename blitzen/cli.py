"""Command-line entry point for blitzen."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time

from .collector import Collector, IntervalReport
from .config import Config
from .geo import km_to_miles
from .protect import PrintEquipment, ProtectionController
from .source import LightningMapsError, LightningMapsSource
from .store import Store, rows_to_dicts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blitzen",
        description="Collect real-time lightning strokes near a fixed location.",
    )
    parser.add_argument("-c", "--config", help="path to config.json")
    parser.add_argument("--lat", type=float, help="override home latitude")
    parser.add_argument("--lon", type=float, help="override home longitude")
    parser.add_argument("--radius-km", type=float, help="override radius in km")
    parser.add_argument(
        "-n", "--interval-minutes", type=float,
        help="reporting interval in minutes (default 5)",
    )
    parser.add_argument("--database", help="override SQLite path")
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="follow the feed and store nearby strokes")
    p_collect.add_argument(
        "--intervals", type=int, default=None,
        help="stop after this many reporting intervals (default: run forever)",
    )
    p_collect.add_argument(
        "--live", action="store_true",
        help="print each nearby stroke as it arrives, not just interval summaries",
    )

    p_probe = sub.add_parser(
        "probe", help="sample the feed briefly and report what it delivers"
    )
    p_probe.add_argument("--seconds", type=float, default=10.0, help="sample duration")

    p_recent = sub.add_parser("recent", help="show stored strokes from the last N minutes")
    p_recent.add_argument("--minutes", type=float, default=60.0)
    p_recent.add_argument("--limit", type=int, default=50)
    p_recent.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    p_protect = sub.add_parser(
        "protect",
        help="cut power to protected equipment when lightning gets close",
    )
    p_protect.add_argument(
        "--trigger-miles", type=float,
        help="cut power inside this many miles (default 10)",
    )
    p_protect.add_argument(
        "--all-clear-minutes", type=float,
        help="restore power after this many quiet minutes (default 30)",
    )
    p_protect.add_argument(
        "--no-fail-safe", action="store_true",
        help="do NOT hold power off when the lightning feed goes blind",
    )
    p_protect.add_argument(
        "--intervals", type=int, default=None,
        help="stop after this many status intervals (default: run forever)",
    )

    p_sim = sub.add_parser(
        "simulate",
        help="exercise the protection state machine offline, with compressed time",
    )
    p_sim.add_argument(
        "--miles", type=float, default=7.0,
        help="distance of the simulated strike (default 7)",
    )
    p_sim.add_argument(
        "--trigger-miles", type=float,
        help="trigger radius to simulate (default 10)",
    )

    sub.add_parser("status", help="show configuration and database summary")

    return parser


def load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    for attr in ("lat", "lon", "radius_km", "interval_minutes", "database"):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(config, attr, value)
    config.validate()
    return config


def cmd_collect(args: argparse.Namespace, config: Config) -> int:
    def report_printer(report: IntervalReport) -> None:
        print(report.summary(config.radius_km), flush=True)

    with Store(config.resolved_database()) as store:
        collector = Collector(
            config,
            store=store,
            on_report=report_printer,
            on_stroke=(lambda n: print(f"  * {n.describe()}", flush=True)) if args.live else None,
        )
        collector.install_signal_handlers()
        print(
            f"blitzen: home {config.lat:.6f},{config.lon:.6f}  "
            f"radius {config.radius_km:.0f} km ({km_to_miles(config.radius_km):.0f} mi)  "
            f"reporting every {config.interval_minutes:g} min\n"
            f"database: {config.resolved_database()}\n"
            f"Ctrl-C to stop.",
            flush=True,
        )
        collector.run(max_intervals=args.intervals)
    return 0


def cmd_probe(args: argparse.Namespace, config: Config) -> int:
    source = LightningMapsSource(source_mask=config.source_mask)
    print(f"probing {source.host} for {args.seconds:.0f}s (source mask {config.source_mask})")

    try:
        primed = source.poll()
    except LightningMapsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"cursor primed at {primed.cursor}; server wait hint {primed.wait_s:.2f}s")

    collector = Collector(config, store=_NullStore(), source=source)
    deadline = time.time() + args.seconds
    total = nearby_total = polls = 0
    nearest = None

    while time.time() < deadline:
        try:
            result = source.poll()
        except LightningMapsError as exc:
            print(f"poll error: {exc}", file=sys.stderr)
            time.sleep(2)
            continue
        polls += 1
        total += len(result.strokes)
        for item in collector.filter_nearby(result.strokes):
            nearby_total += 1
            print(f"  * {item.describe()}")
            if nearest is None or item.distance_km < nearest.distance_km:
                nearest = item
        time.sleep(min(result.wait_s, max(0.0, deadline - time.time())))

    print(
        f"\n{polls} polls, {total} strokes worldwide, "
        f"{nearby_total} within {config.radius_km:.0f} km of "
        f"{config.lat:.6f},{config.lon:.6f}"
    )
    if nearest:
        print(f"nearest: {nearest.describe()}")
    if source.copyright:
        print(f"\n{source.copyright}")
    return 0


def cmd_recent(args: argparse.Namespace, config: Config) -> int:
    since = time.time() - args.minutes * 60.0
    with Store(config.resolved_database()) as store:
        rows = store.strokes_since(since, limit=args.limit)
        total = store.count_since(since)

    if args.json:
        print(json.dumps(rows_to_dicts(rows), indent=2))
        return 0

    if not rows:
        print(f"no strokes stored in the last {args.minutes:g} minutes")
        return 0

    print(f"{total} stroke(s) in the last {args.minutes:g} minutes (showing {len(rows)}):\n")
    print(f"{'time (UTC)':<21}{'km':>8}{'mi':>8}  {'lat':>10}{'lon':>11}{'±m':>8}")
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["time_ms"] / 1000.0))
        dev = f"{row['dev_m']:.0f}" if row["dev_m"] is not None else "-"
        print(
            f"{stamp:<21}{row['distance_km']:>8.1f}{km_to_miles(row['distance_km']):>8.1f}  "
            f"{row['lat']:>10.4f}{row['lon']:>11.4f}{dev:>8}"
        )
    return 0


def cmd_protect(args: argparse.Namespace, config: Config) -> int:
    if args.trigger_miles is not None:
        config.trigger_distance_miles = args.trigger_miles
    if args.all_clear_minutes is not None:
        config.all_clear_minutes = args.all_clear_minutes
    if args.no_fail_safe:
        config.fail_safe_on_stale = False
    config.validate()

    print(
        f"blitzen protect\n"
        f"  location        {config.lat:.6f}, {config.lon:.6f}\n"
        f"  trigger         lightning within {config.trigger_distance_miles:g} mi "
        f"({config.trigger_distance_km:.1f} km)\n"
        f"  all clear       {config.all_clear_minutes:g} min with no strike in radius\n"
        f"  collecting      {config.radius_km:g} km "
        f"({km_to_miles(config.radius_km):.0f} mi) for context\n"
        f"  fail-safe       {'ON  (power held OFF while feed is blind)' if config.fail_safe_on_stale else 'OFF (power stays on while blind)'}\n"
        f"  uncertainty     {'trip on near edge of error circle' if config.use_uncertainty_margin else 'trip on point estimate'}\n"
        f"  status every    {config.interval_minutes:g} min\n"
        f"\nVersion 1 prints what a real switch would do. Ctrl-C to stop.",
        flush=True,
    )

    with Store(config.resolved_database()) as store:
        controller = ProtectionController(config, PrintEquipment(), store=store)
        controller.start()

        def on_report(report: IntervalReport) -> None:
            print(report.summary(config.radius_km), flush=True)
            print(controller.status_line(), flush=True)

        collector = Collector(
            config,
            store=store,
            on_report=on_report,
            on_stroke=controller.note_stroke,
            on_tick=controller.tick,
        )
        collector.install_signal_handlers()
        collector.run(max_intervals=args.intervals)

    print("\nstopped; a real switch would be left in its last commanded state")
    return 0


def cmd_simulate(args: argparse.Namespace, config: Config) -> int:
    """Run the state machine offline against a scripted storm.

    Time is driven by a fake clock so the 30-minute all-clear can be watched in
    a couple of seconds. No network, no database.
    """
    if args.trigger_miles is not None:
        config.trigger_distance_miles = args.trigger_miles
    config.validate()

    now = [time.time()]
    controller = ProtectionController(
        config, PrintEquipment(), store=None, clock=lambda: now[0]
    )

    def advance(minutes: float) -> None:
        now[0] += minutes * 60.0

    print(
        f"simulating: trigger {config.trigger_distance_miles:g} mi, "
        f"all clear {config.all_clear_minutes:g} min, "
        f"strike at {args.miles:g} mi (fake clock, no network)\n"
    )

    print("-- startup: equipment should be held OFF until the feed is confirmed")
    controller.start()

    print("\n-- feed comes up healthy, sky is quiet")
    controller.tick(now[0], now[0])

    print(f"\n-- strike at {args.miles:g} mi")
    controller.note_stroke(_fake_stroke(config, args.miles, now[0]))
    controller.tick(now[0], now[0])

    half = config.all_clear_minutes / 2.0
    print(f"\n-- {half:g} min later, still inside the all-clear window")
    advance(half)
    controller.tick(now[0], now[0])
    print(controller.status_line())

    print(f"\n-- another strike resets the timer")
    controller.note_stroke(_fake_stroke(config, args.miles, now[0]))
    print(controller.status_line())

    print(f"\n-- {config.all_clear_minutes:g} quiet minutes pass")
    advance(config.all_clear_minutes + 0.1)
    controller.tick(now[0], now[0])

    print("\n-- the feed goes blind")
    advance(config.stale_feed_seconds / 60.0 + 1)
    controller.tick(now[0], now[0] - config.stale_feed_seconds - 60)

    print("\n-- the feed recovers")
    controller.tick(now[0], now[0])

    print(f"\nfinal: {controller.status_line().strip()}")
    return 0


def _fake_stroke(config: Config, miles: float, when: float):
    """A stroke due north of home at the requested distance."""
    from .collector import NearbyStroke
    from .geo import EARTH_RADIUS_KM
    from .source import Stroke

    km = miles * 1.609344
    lat = config.lat + math.degrees(km / EARTH_RADIUS_KM)
    stroke = Stroke(src=1, stroke_id=1, time_utc=when, lat=lat, lon=config.lon, dev_m=None)
    return NearbyStroke(stroke, km, 0.0)


def cmd_status(_args: argparse.Namespace, config: Config) -> int:
    print("configuration:")
    for key, value in config.to_dict().items():
        print(f"  {key:<18} {value}")
    print(f"  {'database path':<18} {config.resolved_database()}")

    with Store(config.resolved_database()) as store:
        total = store.total()
        oldest, newest = store.time_range()
    print("\ndatabase:")
    print(f"  {'strokes stored':<18} {total}")
    if oldest and newest:
        fmt = "%Y-%m-%d %H:%M:%SZ"
        print(f"  {'oldest':<18} {time.strftime(fmt, time.gmtime(oldest))}")
        print(f"  {'newest':<18} {time.strftime(fmt, time.gmtime(newest))}")
    return 0


class _NullStore:
    """Stand-in store for ``probe``, which must not write anything."""

    def add_strokes(self, rows) -> int:
        return len(list(rows))

    def prune(self, _older_than_utc: float) -> int:
        return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args)
    except (OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    handlers = {
        "collect": cmd_collect,
        "probe": cmd_probe,
        "protect": cmd_protect,
        "recent": cmd_recent,
        "simulate": cmd_simulate,
        "status": cmd_status,
    }
    try:
        return handlers[args.command](args, config)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
