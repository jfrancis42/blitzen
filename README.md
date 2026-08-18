# blitzen

Cuts power to sensitive electronics when lightning gets close, and restores it
once the storm has passed. Lightning data comes from the LightningMaps.org /
Blitzortung.org community detection network.

Default site is **39.49373180653013, -104.75963833728404** (Parker, Colorado):

* **power off** when a stroke is detected within **10 miles**
* **power on** after **30 quiet minutes** with nothing inside that radius

```bash
bin/blitzen protect
```

Version 1 prints what a real switch would do; nothing is wired up yet. See
[Switching real hardware](#switching-real-hardware).

---

## Install

```bash
cd ~/Dropbox/build/blitzen
pip install -r requirements.txt --break-system-packages
```

Only dependency is `requests`; everything else is standard library. There is
nothing to build and nothing to install — run `bin/blitzen` directly. It works
from any directory, so put it on your PATH if you like:

```bash
ln -s ~/Dropbox/build/blitzen/bin/blitzen ~/bin/blitzen
```

## Use

```bash
# The actual job: watch for lightning and switch the gear
bin/blitzen protect

# Tighter trigger, longer all-clear
bin/blitzen protect --trigger-miles 15 --all-clear-minutes 45

# Watch the whole state machine run in a few seconds -- no network, fake clock
bin/blitzen simulate
bin/blitzen simulate --miles 12 --trigger-miles 15

# Collect and store without switching anything
bin/blitzen collect --live

# What is the feed delivering right now? Writes nothing to the database.
bin/blitzen probe --seconds 20

# What has been stored
bin/blitzen recent --minutes 120
bin/blitzen recent --minutes 60 --json
bin/blitzen status
```

`simulate` is the one to run first — it walks a strike, a timer reset, the
all-clear, and a feed outage in about a second, so you can confirm the logic
does what you expect without waiting for a storm.

A quiet `protect` run is normal and correct. Colorado gets lightning in bursts;
a 10 mile circle is empty the vast majority of the time. To confirm the
plumbing is alive when the sky is quiet:

```bash
bin/blitzen --radius-km 3000 probe --seconds 20
```

## How collection works

The feed is a continuous cursor stream. Each poll returns the strokes appended
since the cursor you last sent back, and the response says how soon to poll
again — currently 500 ms.

blitzen follows that cadence continuously. Every poll is filtered to
`radius_km` around the site, hits are written to SQLite, and anything inside
the trigger radius reaches the protection state machine on the spot. The
reporting interval is separate: every `interval_minutes` blitzen prints a
summary of what it has collected.

| Cadence | What it controls | Default |
|---|---|---|
| Poll interval | how often the feed is read | server's hint (500 ms) |
| Reporting interval | how often a summary is printed | 3 minutes |

Protective switching runs off the poll, not the report, so the reporting
interval can be set to whatever is comfortable to read without affecting how
fast the equipment reacts.

## How the protection logic behaves

```
UNKNOWN  --(feed healthy, no recent strikes)-->  CLEAR    equipment ON
CLEAR    --(strike inside trigger radius)---->   DANGER   equipment OFF
DANGER   --(quiet for all_clear_minutes)----->   CLEAR    equipment ON
```

Two rules sit on top, both chosen so the failure mode is "power off", not
"power on while blind":

* **Starts closed.** On startup the equipment is held OFF until the feed is
  confirmed live *and* the database shows no strike inside the radius within
  the all-clear window. Restarting mid-storm will not re-energise the gear —
  it picks the storm back up from SQLite. In the normal case the OFF window
  lasts about a second.
* **Blind means unsafe.** If the feed stops producing data for
  `stale_feed_seconds`, the equipment goes OFF and stays OFF until data
  resumes. Disable with `--no-fail-safe` if you would rather stay energised
  while blind.

Each strike inside the radius restarts the 30-minute timer; the clock runs from
the *strike* time, not from when blitzen noticed it.

Reported stroke positions carry 2–6 km of uncertainty, which is significant
next to a 16 km trigger radius, so blitzen trips on the near edge of that error
circle rather than the point estimate. Turn it off with
`use_uncertainty_margin` if you want the point estimate instead.

## Configure

`config.json` in the project root:

| Key | Meaning | Default |
|---|---|---|
| `lat`, `lon` | site position, decimal degrees | 39.49373180653013, -104.75963833728404 |
| `radius_km` | keep strokes within this distance | 25 |
| `interval_minutes` | reporting cadence | 3 |
| `poll_seconds` | network poll cadence; `null` follows the server hint | `null` |
| `source_mask` | 2 = Blitzortung, 4 = LightningMaps, 8 = testing; add for both | 6 |
| `database` | SQLite path, relative to the project directory | `blitzen.db` |
| `retain_days` | prune stored strokes older than this; 0 disables | 30 |
| `trigger_distance_miles` | cut power inside this many statute miles | 10 |
| `all_clear_minutes` | quiet minutes required before restoring power | 30 |
| `stale_feed_seconds` | treat the feed as blind after this long without data | 120 |
| `fail_safe_on_stale` | hold power off while blind | `true` |
| `use_uncertainty_margin` | trip on the near edge of the error circle | `true` |

`trigger_distance_miles` must fit inside `radius_km` — strokes beyond the
collection radius are discarded before the trigger ever sees them, so a trigger
wider than collection would silently never fire. blitzen refuses to start in
that configuration rather than pretending to work.

Leave `poll_seconds` as `null` unless you have a reason. Raising it does not
reduce load meaningfully and does risk dropping strokes.

## Data

Each stored stroke:

| Column | Meaning |
|---|---|
| `src`, `stroke_id` | source network and its stroke id (id wraps; not globally unique) |
| `time_ms` | stroke time, ms since epoch UTC |
| `lat`, `lon` | located position |
| `dev_m` | location uncertainty in metres (the site draws a circle of `dev_m / 2`) |
| `delay_ms` | detection-to-publication delay |
| `alt_m`, `server` | altitude and originating server, when present |
| `distance_km`, `bearing_deg` | computed relative to the site at collection time |
| `received_at` | local wall clock when blitzen stored it |

Primary key is `(src, stroke_id, time_ms)`, so re-delivered strokes are ignored
rather than duplicated.

Observed latency is roughly 5–15 seconds from stroke to availability.

## Switching real hardware

`blitzen/protect.py` defines a two-method `Equipment` interface:

```python
class Equipment:
    def power_on(self, reason: str) -> None: ...
    def power_off(self, reason: str) -> None: ...
```

`PrintEquipment` is version 1. A relay driver drops in without touching the
state machine — `~/rf-bench/projects/relay/` already has the XL9535 relay board
working, which is the obvious candidate.

Three things to get right when that happens:

1. **Wire the relay so de-energised means the protected gear is OFF.** Then a
   crash, a power loss, or a pulled cable fails into the safe state instead of
   silently leaving everything connected.
2. **Make `power_on` / `power_off` idempotent.** The controller only calls them
   on an actual change, but a driver should tolerate a repeat.
3. **Decide what a driver exception should do.** Right now an exception in the
   equipment layer would propagate; a real driver should probably retry and
   then latch OFF rather than give up quietly.

## Running under systemd

Not set up yet — run it from a terminal for now. When it moves to a unit file,
the pieces that already fit: it logs to stdout, handles SIGTERM cleanly via
`Collector.install_signal_handlers()`, and recovers storm state from SQLite on
restart, so `Restart=always` will not re-energise the gear mid-storm.

## Layout

```
blitzen/
├── bin/blitzen        entry point -- runs from any directory, no install
├── config.json        default configuration
├── requirements.txt
├── blitzen/
│   ├── source.py      the feed client (protocol notes live in its docstring)
│   ├── collector.py   continuous poll loop, radius filter, interval reports
│   ├── protect.py     protection state machine and equipment interface
│   ├── store.py       SQLite persistence
│   ├── geo.py         haversine / bearing / compass
│   ├── config.py      config dataclass
│   └── cli.py         command-line interface
└── tests/test_blitzen.py
```

## Status

Working: collection, storage, the protection state machine, fail-safe
behaviour, and restart recovery. 30 offline tests.

Not done: an actual relay driver (prints only), and the systemd unit.
