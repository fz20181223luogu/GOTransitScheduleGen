"""Generate combined, date-specific GTFS timetables in Excel and PDF."""

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import List, Tuple, Optional

import gtfs_kit as gk
import pandas as pd

from schedule_ordering import (
    format_time, order_stops, order_trips, time_seconds,
    toposort_from_edges, validate_stop_order,
)

DEFAULT_DUP_MAP = Path(__file__).with_name('duplicate_stn.json')
METROLINX_ATTRIBUTION = (
    'Data used in this product or service is provided with the permission of Metrolinx. '
    'Metrolinx makes no representations or warranties of any kind, express or implied, '
    'with respect to the Data and assumes no responsibility for the accuracy or currency '
    'of the data used in this product or service.'
)


def resolve_route_pairs(feed, target_pairs):
    """Accept exact route IDs or unambiguous public route_short_name values."""
    result = []
    for route, direction in target_pairs:
        if direction not in (0, 1):
            raise ValueError(f'Direction must be 0 or 1: {route!r}, {direction!r}')
        route = str(route)
        matches = feed.routes.loc[feed.routes.route_id.astype(str).eq(route)]
        if matches.empty and 'route_short_name' in feed.routes:
            matches = feed.routes.loc[feed.routes.route_short_name.astype(str).eq(route)]
        if len(matches) != 1:
            raise ValueError(f'Route {route!r} is unknown or ambiguous; specify an exact route_id.')
        pair = (str(matches.iloc[0].route_id), int(direction))
        if pair not in result:
            result.append(pair)
    if not result:
        raise ValueError('Select at least one route/direction pair.')
    return result


def generate_combined_schedule(
    feed: gk.Feed,
    target_date: str,
    target_pairs: List[Tuple[str, int]],
    dup_map_path: Optional[str] = DEFAULT_DUP_MAP,
    reverse: bool = False,
    verbose: bool = False,
    *,
    stop_ids: Optional[List[str]] = None,
    trip_sort_stop: Optional[str] = None,
) -> pd.DataFrame:
    """Build one timetable, with trips as rows and named stops as columns.

    The first active selected route defines the reference corridor. ``stop_ids``
    supplies an exact list of canonical stop IDs in forward travel order; it is
    reversed automatically for the reverse sheet. Known but inactive stops stay
    as empty columns. Trips with no selected stops are omitted. ``trip_sort_stop``
    selects the reference timepoint (default: the busiest displayed stop).
    """
    if len(target_date) != 8 or not target_date.isdigit():
        raise ValueError('target_date must use YYYYMMDD format.')
    datetime.strptime(target_date, '%Y%m%d')
    target_pairs = resolve_route_pairs(feed, target_pairs)
    if reverse:
        target_pairs = [(rid, 1 - did) for rid, did in target_pairs]
    stop_map = dict(zip(feed.stops.stop_id.astype(str), feed.stops.stop_name))
    if stop_ids is not None:
        stop_ids = [str(s) for s in stop_ids]
        if not stop_ids or len(set(stop_ids)) != len(stop_ids):
            raise ValueError('stop_ids must be a nonempty list without duplicate IDs.')
        unknown = sorted(set(stop_ids) - stop_map.keys())
        if unknown:
            raise ValueError(f'Unknown stop_ids: {", ".join(unknown)}')
        if reverse:
            stop_ids = list(reversed(stop_ids))
    if trip_sort_stop is not None:
        trip_sort_stop = str(trip_sort_stop)
        if trip_sort_stop not in stop_map:
            raise ValueError(f'Unknown trip sorting stop: {trip_sort_stop}')

    def empty_schedule():
        stops = stop_ids or []
        empty = pd.DataFrame(columns=['trip_headsign', 'trip_id'] + display_stop_names(stops, stop_map))
        empty.attrs.update(stop_ids=stops, target_date=target_date, reverse=reverse, trip_keys=[])
        return empty

    timetable_list = []
    unique_route_ids = list(dict.fromkeys(rid for rid, _ in target_pairs))
    for rid in unique_route_ids:
        # Do not suppress GTFS/parser errors as an apparently empty timetable.
        df_route = feed.build_route_timetable(rid, [target_date])
        if df_route is not None and not df_route.empty:
            timetable_list.append(df_route)
    if not timetable_list:
        return empty_schedule()
    raw_merged = pd.concat(timetable_list, ignore_index=True)
    if 'trip_headsign' not in raw_merged:
        raw_merged['trip_headsign'] = ''
    mask = pd.MultiIndex.from_frame(raw_merged[['route_id', 'direction_id']]).isin(target_pairs)
    matrix_data = raw_merged.loc[mask, [
        'route_id', 'trip_id', 'stop_id', 'departure_time', 'stop_sequence', 'trip_headsign'
    ]].drop_duplicates().copy()
    if matrix_data.empty:
        return empty_schedule()
    for column in ['stop_id', 'route_id', 'trip_id']:
        matrix_data[column] = matrix_data[column].astype(str)
    matrix_data['stop_sequence'] = pd.to_numeric(matrix_data.stop_sequence, errors='raise')
    dup_map_raw = None
    if dup_map_path:
        with open(dup_map_path, encoding='utf-8') as source:
            dup_map_raw = json.load(source)
        # Also accept duplicate_stn_gen.py's flat structured output.
        if isinstance(dup_map_raw, list) and dup_map_raw and all(
            isinstance(entry, dict) and 'duplicate_stop_id' in entry for entry in dup_map_raw
        ):
            dup_map_raw = [dup_map_raw]
    def _build_type_map(raw_entry):
        """Build a mapping dup_id -> (canonical_id, priority) from a raw type entry.
        raw_entry can be:
          - dict: {dup_id: canonical_id}
          - list of structured dicts [{'duplicate_stop_id':..., 'canonical_stop_id':..., 'priority':...}, ...]
          - list of small dicts [{dup_id: canonical_id}, ...]
        """
        res = {}
        if not raw_entry:
            return res
        if isinstance(raw_entry, dict):
            for k, v in raw_entry.items():
                res[k] = (v, 50)  # default priority for legacy dict entries
            return res
        if isinstance(raw_entry, list):
            for item in raw_entry:
                if not isinstance(item, dict):
                    continue
                # structured item with explicit fields
                if 'duplicate_stop_id' in item and 'canonical_stop_id' in item:
                    dup = item['duplicate_stop_id']
                    can = item['canonical_stop_id']
                    pri = int(item.get('priority', 50)) if item.get('priority') is not None else 50
                    res[dup] = (can, pri)
                else:
                    # treat single-key dict as {dup: canonical}
                    if len(item) == 1:
                        k, v = next(iter(item.items()))
                        res[k] = (v, 50)
            return res
        return res

    if isinstance(dup_map_raw, dict):
        # legacy behavior: single mapping applied once
        matrix_data['stop_id'] = matrix_data['stop_id'].map(lambda s: dup_map_raw.get(s, s))
    elif isinstance(dup_map_raw, list):
        # priority-based existence-aware mapping per type index 0..n
        n_types = len(dup_map_raw)
        for t in range(n_types):
            raw_entry = dup_map_raw[t] if t < len(dup_map_raw) else None
            type_map = _build_type_map(raw_entry)
            if not type_map:
                continue
            # Apply this type's mappings per trip, using priority-aware conflict resolution
            new_rows = []
            grouped = matrix_data.groupby('trip_id', sort=False)
            for tid, grp in grouped:
                grp = grp.sort_values('stop_sequence')
                seen = {}  # canonical_stop_id -> (priority, index_in_trip_rows)
                trip_rows = []
                for _, r in grp.iterrows():
                    orig_stop = r['stop_id']
                    if orig_stop in type_map:
                        mapped, p = type_map[orig_stop]
                    else:
                        mapped = orig_stop
                        # original, unmapped stops get very high self-priority to avoid being overridden
                        p = 1000
                    if mapped not in seen:
                        # first occurrence of this canonical stop in this trip; add it
                        new_r = r.copy()
                        new_r['stop_id'] = mapped
                        trip_rows.append(new_r)
                        # store priority, index, and the original stop id that produced this mapping
                        seen[mapped] = (p, len(trip_rows) - 1, orig_stop)
                    else:
                        existing_p, existing_idx, existing_orig = seen[mapped]
                        if p > existing_p:
                            # replace earlier occurrence with this one, but route the earlier occurrence back to its original stop id
                            try:
                                # mutate the earlier row's stop_id back to its original
                                trip_rows[existing_idx]['stop_id'] = existing_orig
                            except Exception:
                                # if mutation fails, fall back to popping the earlier row and adjust indices
                                trip_rows.pop(existing_idx)
                                for kkk, (ppp, idxx, ooo) in list(seen.items()):
                                    if idxx > existing_idx:
                                        seen[kkk] = (ppp, idxx - 1, ooo)
                            # append current mapped occurrence
                            new_r = r.copy()
                            new_r['stop_id'] = mapped
                            trip_rows.append(new_r)
                            # update seen to point to the new occurrence (store its original producer)
                            seen[mapped] = (p, len(trip_rows) - 1, orig_stop)
                            if verbose:
                                print(f"[DEBUG] Replacing mapped stop and routing old back: {orig_stop} -> {mapped} (old routed to {existing_orig})")
                        else:
                            # Do not drop times: keep this occurrence as its original stop id
                            new_r = r.copy()
                            new_r['stop_id'] = orig_stop
                            trip_rows.append(new_r)
                            # do not alter seen (canonical remains pointing to the earlier mapped occurrence)
                            if verbose:
                                print(f"[DEBUG] Keeping unmapped stop: {orig_stop}")
                new_rows.extend(trip_rows)
            # rebuild matrix_data preserving original columns
            if new_rows:
                matrix_data = pd.DataFrame(new_rows, columns=matrix_data.columns)
            else:
                # nothing mapped; keep existing
                pass
    else:
        # no duplicate map found; continue without changes
        pass

    matrix_data['stop_id'] = matrix_data.stop_id.astype(str)
    # Restrict the displayed network before ordering so a selected subset can
    # intentionally exclude a loop or an incompatible branch.
    visible = matrix_data if stop_ids is None else matrix_data[matrix_data.stop_id.isin(stop_ids)]
    if visible.empty:
        return empty_schedule()
    matrix_data = matrix_data[matrix_data.trip_id.isin(visible.trip_id.unique())].copy()
    union_ids = [s for s in visible.stop_id.unique()
                 if str(stop_map.get(s, '')).strip().casefold() == 'union station go']
    if stop_ids is None:
        ordered_stops = order_stops(visible, unique_route_ids, priority_node=min(union_ids, default=None))
    else:
        validate_stop_order(visible, stop_ids)
        ordered_stops = stop_ids

    # Keep multiple calls visible and sort their raw service-day values before
    # formatting. Never parse a space-joined collection of times as one duration.
    matrix_data['_seconds'] = matrix_data.departure_time.map(time_seconds)
    numeric = matrix_data.groupby(['stop_id', 'route_id', 'trip_id'])['_seconds'].min().unstack(['route_id', 'trip_id'])
    trip_keys = sorted(set(zip(matrix_data.route_id, matrix_data.trip_id)))
    numeric = numeric.reindex(columns=pd.MultiIndex.from_tuples(trip_keys, names=['route_id', 'trip_id']))
    displayed_times = numeric.reindex(ordered_stops)
    sorting_times = displayed_times
    if trip_sort_stop is not None and trip_sort_stop not in sorting_times.index:
        sorting_times = pd.concat([sorting_times, numeric.reindex([trip_sort_stop])])
    ordered_trips, anchor = order_trips(
        sorting_times, trip_sort_stop, start_times=numeric.min(axis=0), verbose=verbose,
    )

    def join_times(values):
        valid = sorted({str(v) for v in values if pd.notna(v) and str(v).strip()}, key=time_seconds)
        return ' / '.join(format_time(v) for v in valid)

    timetable = matrix_data.groupby(['stop_id', 'route_id', 'trip_id']).departure_time.agg(
        join_times
    ).unstack(['route_id', 'trip_id']).reindex(
        index=ordered_stops, columns=pd.MultiIndex.from_tuples(ordered_trips)
    ).T
    timetable = timetable.fillna('').astype(object)
    # Arrows mean no scheduled call in this column, not a physical path through
    # a branch and not a transfer guarantee.
    active_stops = set(visible.stop_id)
    for row in range(len(timetable)):
        calls = [i for i, value in enumerate(timetable.iloc[row]) if value]
        if calls:
            for column in range(calls[0], calls[-1] + 1):
                if not timetable.iat[row, column] and ordered_stops[column] in active_stops:
                    timetable.iat[row, column] = '→'
    timetable.columns = display_stop_names(ordered_stops, stop_map)
    headsigns = matrix_data.set_index('trip_id').trip_headsign.to_dict()
    route_names = feed.routes.set_index('route_id').route_short_name.to_dict()
    route_labels, trip_labels = [], []
    for route, trip in ordered_trips:
        headsign = headsigns.get(trip)
        first_word = headsign.split()[0] if isinstance(headsign, str) and headsign.strip() else ''
        route_labels.append(first_word if first_word[:1].isdigit() else str(route_names.get(route, route)))
        trip_labels.append(trip.rsplit('-', 1)[-1])
    timetable = timetable.reset_index(drop=True)
    timetable.insert(0, 'trip_id', trip_labels)
    timetable.insert(0, 'trip_headsign', route_labels)
    timetable.attrs.update(
        stop_ids=ordered_stops, target_date=target_date, reverse=reverse,
        trip_keys=ordered_trips, trip_sort_stop=anchor,
    )
    return timetable


def display_stop_names(stops, stop_map):
    """Give repeated names stable, unambiguous labels."""
    result, seen = [], {'trip_headsign', 'trip_id'}
    for stop in stops:
        name = str(stop_map.get(stop, stop))
        if name in seen:
            name = f'{name} ({stop})'
        result.append(name)
        seen.add(name)
    return result


def generate_both_directions(
    feed: gk.Feed,
    target_date: str,
    target_pairs: List[Tuple[str, int]],
    dup_map_path: Optional[str] = DEFAULT_DUP_MAP,
    out_excel: str = 'combined_routes_schedule.xlsx',
    verbose: bool = False,
    *,
    stop_ids: Optional[List[str]] = None,
    trip_sort_stop: Optional[str] = None,
    out_pdf: Optional[str] = None,
    title: str = 'Combined transit timetable',
):
    """Write both directions to Excel and optionally a paginated PDF.

    Return the generated DataFrames as a {'forward': ..., 'reverse': ...} dict.
    """
    schedules = {
        name: generate_combined_schedule(
            feed, target_date, target_pairs, dup_map_path, reverse, verbose,
            stop_ids=stop_ids, trip_sort_stop=trip_sort_stop,
        ) for name, reverse in [('forward', False), ('reverse', True)]
    }
    attribution = None
    if feed.agency is not None and 'agency_name' in feed.agency:
        if feed.agency.agency_name.astype(str).str.casefold().eq('go transit').any():
            attribution = METROLINX_ATTRIBUTION
    Path(out_excel).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_excel, engine='xlsxwriter') as writer:
        header = writer.book.add_format({
            'rotation': 90, 'bold': True, 'bg_color': '#E8EFEA', 'valign': 'vcenter',
            'border': 1, 'text_wrap': True,
        })
        for name, frame in schedules.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes(1, 2)
            sheet.set_column(0, 0, 10)
            sheet.set_column(1, 1, 12)
            sheet.set_column(2, max(2, len(frame.columns) - 1), 12)
            sheet.set_row(0, 180)
            for column, label in enumerate(frame.columns):
                sheet.write(0, column, label, header)
            sheet.autofilter(0, 0, len(frame), len(frame.columns) - 1)
            sheet.repeat_rows(0)
            sheet.repeat_columns(0, 1)
            sheet.set_landscape()
            sheet.fit_to_pages(1, 0)
        notes = [
            f'Service date: {target_date}',
            'Times use the GTFS agency timezone. +1 means the next calendar day.',
            'An arrow means no scheduled call in that column; it does not establish a physical route or transfer.',
            'Forward uses the selected direction IDs; reverse flips each ID.',
            'Independently generated timetable. Consult the operator for current service.',
        ]
        if attribution:
            notes.append(attribution)
        legend = pd.DataFrame({'Notes': notes})
        legend.to_excel(writer, sheet_name='notes', index=False)
        writer.sheets['notes'].set_column(0, 0, 120)
    if out_pdf:
        from schedule_pdf import export_schedule_pdf
        export_schedule_pdf(schedules, out_pdf, target_date=target_date, title=title, attribution=attribution)
    return schedules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed', required=True, help='GTFS ZIP file or directory')
    parser.add_argument('--date', required=True, help='Service date in YYYYMMDD format')
    parser.add_argument('--routes', nargs='+', required=True, metavar='ROUTE:DIRECTION',
                        help='Route ID or short name, e.g. LW:1 11:0 12:0 16:0 18:0')
    parser.add_argument('--stops', nargs='+', help='Exact canonical stop IDs in forward order')
    parser.add_argument('--sort-stop', help='Canonical stop ID used to order trips')
    parser.add_argument('--duplicates', default=str(DEFAULT_DUP_MAP), help='Duplicate stop JSON; empty string disables it')
    parser.add_argument('--excel', default='output/combined_routes_schedule.xlsx')
    parser.add_argument('--pdf', help='Optional PDF output path')
    parser.add_argument('--title', default='Combined transit timetable')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    try:
        pairs = [(route, int(direction)) for route, direction in (p.rsplit(':', 1) for p in args.routes)]
        feed = gk.read_feed(args.feed, dist_units='km')
        schedules = generate_both_directions(
            feed, args.date, pairs, args.duplicates or None, args.excel, args.verbose,
            stop_ids=args.stops, trip_sort_stop=args.sort_stop, out_pdf=args.pdf, title=args.title,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f'Excel: {args.excel}')
    if args.pdf:
        print(f'PDF: {args.pdf}')
    for direction, schedule in schedules.items():
        print(f'{direction}: {len(schedule)} trips, {max(0, len(schedule.columns) - 2)} stops')


if __name__ == '__main__':
    main()
