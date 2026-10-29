# GO Transit Schedule Generator

Generate combined train/bus timetable grids from a local GTFS feed for one
service date. Export both travel directions to Excel and optionally a printable
PDF. Trips occupy rows and stops occupy columns, as in a published schedule.

## Setup

Python 3.10 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Download a GTFS ZIP from the [official GO Transit developer page](https://www.gotransit.com/en/partner-with-us/software-developers).
Keep it outside the repository, for example `../data/GO-GTFS.zip`. Choose a date
covered by that feed. GO publishes updated feeds regularly; stop and route IDs
may change between releases.

## Generate Excel and PDF

Run commands from this repository directory:

```sh
python ScheduleGen.py \
  --feed ../data/GO-GTFS.zip \
  --date 20261029 \
  --routes LW:1 11:0 12:0 16:0 18:0 \
  --excel output/lakeshore-west.xlsx \
  --pdf output/pdf/lakeshore-west.pdf \
  --title 'Lakeshore West | LW, 11, 12, 16, 18'
```

Route selectors accept an exact `route_id` or an unambiguous `route_short_name`.
The suffix is the route's GTFS `direction_id`, **not** a shared east/west flag:
in this GO feed, inbound LW is `1` and inbound buses are `0`. Reverse output
flips each selected direction. The first selected active route defines the
reference corridor, so put `LW` first in this example.

Excel contains `forward`, `reverse`, and `notes` sheets. PDF output uses
landscape A3 pages, repeating station headers, route/trip identifiers and page
numbers. Tables wider than 32 stops are split into labeled station panels,
which repeat the same group of trips. Omitting `--pdf` produces Excel only.

## Choose stops and a sorting timepoint

```sh
python ScheduleGen.py \
  --feed ../data/GO-GTFS.zip --date 20261029 \
  --routes LW:1 11:0 12:0 16:0 18:0 \
  --stops NI SCTH CF WR HA AL BU 00139 AP BO OA CL PO LO MI EX UN \
  --sort-stop UN \
  --excel output/selected-stops.xlsx \
  --pdf output/pdf/selected-stops.pdf
```

`--stops` is an **exact ordered list**, not a set of sorting hints. Use canonical
stop IDs after applying `duplicate_stn.json`; for example, `02300` maps to `UN`
and `02408` maps to `NI`. In the tested feed, `00139` is Dundas St. / Hwy. 407.
The reverse table reverses this list. Known stops without service remain blank;
trips that serve none of the selected stops are omitted. Unknown IDs, duplicate
IDs, and a column order contradicting a trip's travel direction are rejected.
Use the current feed's `stops.txt` to identify other stops.

`--sort-stop` chooses the station whose departure times take precedence when
trips overtake each other. It may be hidden from the displayed stop list, but
must have timed calls among the retained trips. Without it, the busiest displayed
stop is used (ties follow displayed station order). Trips serving no common
sorting timepoint fall back to their first departure, then stable route/trip IDs.
This orders trips; it does not match transfers.

## Python API

```python
import gtfs_kit as gk
from ScheduleGen import generate_combined_schedule, generate_both_directions

feed = gk.read_feed('../data/GO-GTFS.zip', dist_units='km')
routes = [('LW', 1), ('11', 0), ('12', 0), ('16', 0), ('18', 0)]

table = generate_combined_schedule(
    feed, '20261029', routes,
    stop_ids=['NI', 'SCTH', 'CF', 'WR', 'AL', 'BU', 'UN'],
    trip_sort_stop='UN',
)

schedules = generate_both_directions(
    feed, '20261029', routes,
    out_excel='output/schedule.xlsx',
    out_pdf='output/pdf/schedule.pdf',
)
```

Existing positional arguments remain supported. New options are keyword-only.
The default duplicate map is resolved relative to `ScheduleGen.py`, so imports
work from other working directories. Pass `dup_map_path=None` (CLI:
`--duplicates ''`) to disable mapping, or provide a custom JSON file.
Legacy dictionaries, per-type mapping lists, and the flat structured list from
`duplicate_stn_gen.py` are supported. Existing conflict-priority rules are kept.

Returned DataFrames include `stop_ids`, `trip_keys`, `target_date`, `reverse`, and
the chosen `trip_sort_stop` in `.attrs` (the timepoint is absent for empty output).
These retain full internal identifiers even though column labels and trip IDs
are shortened for presentation.

## Ordering and limitations

- Station order uses actual GTFS `stop_sequence` constraints. The longest path
  through the first active route's combined patterns defines the corridor,
  including express-only endpoints. Branches are inserted next to their shared
  corridor stops. Straight-line distance is not used. Union is kept at an
  endpoint when the stop graph permits it.
- All observed station precedence is preserved. Opposing patterns or loops that
  cannot fit a single column per station produce a clear error instead of an
  unreliable fallback. Select a compatible subset or separate the routes.
- Trip ordering prioritizes the reference timepoint, then other shared stations
  by coverage. Equal departures allow downstream tie-breaking. Overtaking does
  not drop trips or introduce cyclic sorting constraints.
- GTFS service-day hours are preserved while sorting. `25:10:00` displays as
  `01:10+1`, distinguishing it from the start of the same service day. Repeated
  adjacent calls are retained; PDF cells support up to two times.
- Times are scheduled **departures**, in the agency's timezone. Calendar and
  calendar-date exceptions are handled by `gtfs-kit`. No-service selections
  produce an empty sheet and an explicit no-service PDF page.
- An arrow means no scheduled call in that displayed column, not a physical
  path through a branch. Connection matching, guaranteed transfers, separate
  arrival/departure columns, and frequency-based service expansion are not
  implemented. This generator targets scheduled GO feed trips.

These are independently generated timetables, not official GO Transit PDFs.
The official developer page defines the feed's access and use terms. GO exports
include the required Metrolinx data attribution; other feeds retain their own
licensing requirements.

## Verification

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use small GTFS fixtures without network access and cover curved corridors,
express/local patterns, branching, overtaking, equal-time departures, midnight,
duplicate station mapping, exact stop selection, calendar-date service, empty
directions, and both axes of PDF pagination.
