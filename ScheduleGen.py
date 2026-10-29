"""
Refactored schedule generator
- Encapsulates timetable creation in generate_combined_schedule
- Adds generate_both_directions to produce forward/reverse sheets in one Excel file
- Preserves duplicate stop handling, spatial station toposort, trip ordering, time normalization, and arrow filling
- Uses gtfs_kit for GTFS access
"""

import gtfs_kit as gk
import pandas as pd
import json
from collections import defaultdict, deque
import numpy as np
from typing import List, Tuple, Optional, Dict, Any

def toposort_from_edges(
    nodes,
    edges,
    coords: Optional[Dict[str, Tuple[float, float]]] = None,
    priority_node: Optional[str] = None,
):
    """Topological sort with global spatial orientation (Option 2 variant).
    
    Identifies the two furthest terminal points as global anchors to resolve
    ordering alignment across multiple starting/ending terminal branches.
    A prioritized endpoint keeps its unbranched path at the corresponding end.
    """
    adj = defaultdict(set)
    indeg = defaultdict(int)
    for n in nodes:
        indeg[n] = 0
    for u, v in edges:
        if v not in adj[u]:
            adj[u].add(v)
            indeg[v] += 1
            indeg.setdefault(u, indeg.get(u, 0))

    def haversine(a, b):
        if a is None or b is None:
            return float('inf')
        from math import radians, sin, cos, asin, sqrt
        lat1, lon1 = a
        lat2, lon2 = b
        lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        s = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        return 2 * 6371 * asin(sqrt(s))

    coords_map = coords or {}
    
    # 1. Precompute the absolute extreme terminal ends to define global travel trajectory
    global_start_node = None
    global_end_node = None
    max_distance = -1.0
    
    spatial_nodes = [n for n in nodes if n in coords_map]
    if len(spatial_nodes) >= 2:
        for i in range(len(spatial_nodes)):
            for j in range(i + 1, len(spatial_nodes)):
                n1, n2 = spatial_nodes[i], spatial_nodes[j]
                d = haversine(coords_map[n1], coords_map[n2])
                if d > max_distance:
                    max_distance = d
                    # Orient start/end using the topological direction network
                    def can_reach(start, target):
                        visited = set()
                        queue = deque([start])
                        while queue:
                            curr = queue.popleft()
                            if curr == target:
                                return True
                            for nbr in adj[curr]:
                                if nbr not in visited:
                                    visited.add(nbr)
                                    queue.append(nbr)
                        return False
                    
                    if can_reach(n1, n2):
                        global_start_node, global_end_node = n1, n2
                    elif can_reach(n2, n1):
                        global_start_node, global_end_node = n2, n1
                    else:
                        # Fallback if unlinked parallel paths: order by string sort for stability
                        global_start_node, global_end_node = sorted([n1, n2])[0], sorted([n1, n2])[1]

    global_start_coord = coords_map.get(global_start_node) if global_start_node else None
    global_end_coord = coords_map.get(global_end_node) if global_end_node else None

    initial_indeg = indeg.copy()
    predecessors = defaultdict(set)
    for u, neighbors in adj.items():
        for v in neighbors:
            predecessors[v].add(u)

    initial_zero_nodes = [n for n, d in initial_indeg.items() if d == 0]
    terminal_nodes = [n for n in indeg if not adj[n]]
    priority_prefix = []
    priority_suffix = set()

    if priority_node in initial_zero_nodes and len(initial_zero_nodes) > 1:
        current = priority_node
        priority_prefix.append(current)
        while len(adj[current]) == 1:
            next_node = next(iter(adj[current]))
            if initial_indeg[next_node] > 1:
                break
            priority_prefix.append(next_node)
            if len(adj[next_node]) > 1:
                break
            current = next_node
    elif priority_node in terminal_nodes and len(terminal_nodes) > 1:
        current = priority_node
        priority_suffix.add(current)
        while len(predecessors[current]) == 1:
            previous = next(iter(predecessors[current]))
            if initial_indeg[previous] > 1 or len(adj[previous]) > 1:
                break
            priority_suffix.add(previous)
            current = previous

    zero_nodes = list(initial_zero_nodes)
    topo = []
    
    while zero_nodes:
        if priority_prefix and priority_prefix[0] in zero_nodes:
            pick = priority_prefix.pop(0)
        else:
            candidates = zero_nodes
            non_priority_nodes = [n for n in zero_nodes if n not in priority_suffix]
            if priority_suffix and non_priority_nodes:
                candidates = non_priority_nodes

            has_spatial_data = coords_map and any(n in coords_map for n in candidates)
        
            if len(candidates) == 1 or not has_spatial_data or not global_start_coord:
                pick = sorted(candidates)[0]
            else:
                progress_scores = {}
                for node in candidates:
                    node_coord = coords_map.get(node)
                    if not node_coord:
                        progress_scores[node] = float('inf')
                        continue
                    
                    # 2. Score progress based on relative position along the global travel axis
                    dist_from_start = haversine(global_start_coord, node_coord)
                    dist_to_end = haversine(node_coord, global_end_coord) if global_end_coord else 0.0
                    
                    total_axis_dist = dist_from_start + dist_to_end
                    if total_axis_dist == 0:
                        progress_scores[node] = 0.0
                    else:
                        progress_scores[node] = dist_from_start / total_axis_dist
                
                # Pop the candidate closest to the global starting terminal
                pick = min(candidates, key=lambda n: (progress_scores.get(n, float('inf')), n))
            
        zero_nodes.remove(pick)
        topo.append(pick)
        
        for nbr in sorted(adj.get(pick, [])):
            indeg[nbr] -= 1
            if indeg[nbr] == 0:
                zero_nodes.append(nbr)
                
    if len(topo) != len(indeg):
        return None
    return topo

def generate_combined_schedule(
    feed: gk.Feed,
    target_date: str,
    target_pairs: List[Tuple[str, int]],
    dup_map_path: Optional[str] = 'duplicate_stn.json',
    reverse: bool = False,
    verbose: bool = False,
) -> pd.DataFrame:
    """Build a combined timetable DataFrame for the specified route/direction pairs.

    Returns a DataFrame ready for export (rows = trips, columns = stops), with internal
    columns kept as stop_id until export.
    """

    # Reverse direction if requested
    if reverse:
        target_pairs = [(rid, 1 - did) for rid, did in target_pairs]

    # Use native build_route_timetable to auto-merge active dates, shapes, and columns
    timetable_list = []
    unique_route_ids = list({rid for rid, _ in target_pairs})
    for rid in unique_route_ids:
        try:
            df_route = feed.build_route_timetable(rid, [target_date])
            if df_route is not None and not df_route.empty:
                timetable_list.append(df_route)
        except Exception:
            continue

    if not timetable_list:
        return pd.DataFrame()

    raw_merged = pd.concat(timetable_list, ignore_index=True)
    mask = pd.MultiIndex.from_frame(raw_merged[["route_id", "direction_id"]]).isin(target_pairs)
    matrix_data = raw_merged[mask][['route_id', 'trip_id', 'stop_id', 'departure_time', 'stop_sequence', 'trip_headsign']].drop_duplicates()

    # apply duplicate stop mapping early so ordering merges correctly
    # The duplicate map may be a legacy dict {dup: canonical} or a list of override dicts/lists by type index [type0, type1, type2, type3].
    dup_map_raw = None
    try:
        if dup_map_path:
            with open(dup_map_path, 'r', encoding='utf-8') as f:
                dup_map_raw = json.load(f)
    except Exception:
        dup_map_raw = None

    matrix_data = matrix_data.copy()

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

    # build station precedence edges from per-trip consecutive stops
    all_stop_ids = matrix_data['stop_id'].astype(str).unique().tolist()
    edges = []
    for tid, grp in matrix_data.groupby('trip_id'):
        try:
            seq = grp.sort_values('stop_sequence')['stop_id'].astype(str).tolist()
            for a, b in zip(seq, seq[1:]):
                if a != b:
                    edges.append((a, b))
        except Exception:
            continue

    # gtfs-kit structures feed.stops reliably as a standard DataFrame on ingest
    stop_coords = {}
    if feed.stops is not None and 'stop_lat' in feed.stops.columns:
        stop_coords = feed.stops.set_index('stop_id')[['stop_lat', 'stop_lon']].apply(
            lambda r: (float(r['stop_lat']), float(r['stop_lon'])), axis=1
        ).to_dict()


    union_stop_id = None
    if feed.stops is not None and {'stop_id', 'stop_name'}.issubset(feed.stops.columns):
        union_stop_ids = feed.stops.loc[
            feed.stops['stop_name'].astype(str).str.strip().str.casefold().eq('union station go'),
            'stop_id',
        ].astype(str)
        union_stop_id = next((stop_id for stop_id in union_stop_ids if stop_id in all_stop_ids), None)

    ordered_stops = toposort_from_edges(
        all_stop_ids,
        edges,
        coords=stop_coords,
        priority_node=union_stop_id,
    )
    if verbose:
        print(f"[DEBUG] computed station edges: {len(edges)} edges, {len(all_stop_ids)} nodes")
    if ordered_stops is None:
        if verbose:
            print("[DEBUG] station toposort detected cycle; falling back to min stop_sequence ordering")
        min_seq = matrix_data.groupby('stop_id')['stop_sequence'].min()
        ordered_stops = list(min_seq.sort_values().index.astype(str))
    remaining = [s for s in all_stop_ids if s not in ordered_stops]
    if remaining:
        try:
            min_seq = matrix_data.groupby('stop_id')['stop_sequence'].min()
            remaining_sorted = [s for s in min_seq.sort_values().index.astype(str) if s in remaining]
        except Exception:
            remaining_sorted = sorted(remaining)
        ordered_stops = ordered_stops + remaining_sorted

    if verbose:
        print(f"[DEBUG] ordered_stops length={len(ordered_stops)}")

    # Pivot matrix
    try:
        timetable_matrix = matrix_data.pivot(index='stop_id', columns=['route_id', 'trip_id'], values='departure_time')
    except ValueError: # MultiIndex pivot error due to duplicate entries; fallback to aggregation
        timetable_matrix = matrix_data.pivot_table(index='stop_id', columns=['route_id', 'trip_id'], values='departure_time', aggfunc = lambda x: " ".join(x))
        if verbose:
            print("[DEBUG] pivot_table fallback used due to duplicate entries. Check original GTFS data and duplicate station database.")
    timetable_matrix = timetable_matrix.reindex(ordered_stops)
    
    # =========================================================================
    # CYCLE-PREVENTING SEQUENTIAL TRIP PRECEDENCE LOGIC (SAFE FOR OVERTAKING)
    # =========================================================================
    def introduces_cycle(u, v, adjacency_dict):
        """Returns True if adding edge u -> v creates a directed cycle."""
        if u == v:
            return True
        queue = deque([v])
        visited = {v}
        while queue:
            curr = queue.popleft()
            if curr == u:
                return True
            for neighbor in adjacency_dict[curr]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        return False

    col_nodes = list(timetable_matrix.columns)
    trip_edges = []
    trip_adj = defaultdict(set)  # Internal lookup for on-the-fly cycle detection

    # Convert entire matrix to timedeltas at once for lightning-fast lookups
    td_matrix = timetable_matrix.map(pd.to_timedelta, errors='coerce')

    for stop in ordered_stops:
        if stop not in td_matrix.index:
            continue
        row = td_matrix.loc[stop]
        # Build entries directly from valid timedelta values
        entries = [(node, val) for node, val in row.items() if pd.notna(val)]
        if not entries:
            continue

        
        # Sort trips strictly chronologically at THIS specific station
        entries.sort(key=lambda x: (x[1], x[0]))
        n = len(entries)
        
        # Build sequential links only between adjacent departures (i -> i+1)
        for i in range(n - 1):
            a = entries[i][0]
            b = entries[i + 1][0]
            if a == b:
                continue
            
            # If the chronological edge doesn't exist yet, check before insertion
            if b not in trip_adj[a]:
                if not introduces_cycle(a, b, trip_adj):
                    trip_adj[a].add(b)
                    trip_edges.append((a, b))
                elif verbose:
                    print(f"[DEBUG] Overtaking event handled: skipped edge {a} -> {b} at stop {stop} to prevent a graph cycle.")

    if verbose:
        print(f"[DEBUG] built trip edges: {len(trip_edges)} edges")

    # # Run toposort on our filtered, cycle-free graph setup
    ordered_columns = toposort_from_edges(col_nodes, trip_edges)

    if verbose:
        if ordered_columns is None:
            print("[DEBUG] trip toposort failed (cycle)")
        else:
            print(f"[DEBUG] ordered_columns length={len(ordered_columns)}")
            print("[DEBUG] sample ordered columns:", [c for c in ordered_columns[:20]])
    # Clean up column configurations smoothly using your pre-computed stats
    if ordered_columns is None:
        ordered_columns = []
        
    remaining = [n for n in col_nodes if n not in ordered_columns]
    if remaining:
        trip_stats = feed.compute_trip_stats([target_date])
        trip_start_map = trip_stats.set_index('trip_id')['start_time'].to_dict()
        remaining_sorted = sorted(remaining, key=lambda n: pd.to_timedelta(trip_start_map.get(n[1], "99:99:99")))
        ordered_columns = ordered_columns + remaining_sorted


    # reorder and transpose
    multi_route_schedule = timetable_matrix[ordered_columns].T

    # Map Trip Headsigns smoothly (Data already pulled securely via build_route_timetable)
    trips_map = matrix_data.set_index('trip_id')['trip_headsign'].to_dict()

    # rename index (trip_headsign, short_trip)
    new_index_tuples = []
    for rid, tid in multi_route_schedule.index:
        headsign = trips_map.get(tid, rid)
        try:
            headsign_short = (headsign.split()[0] if isinstance(headsign, str) else headsign)
            if headsign_short and not headsign_short[0].isdigit():
                headsign_short = None
        except Exception:
            headsign_short = None
        tid_short = tid.split('-')[-1] if isinstance(tid, str) and '-' in tid else tid
        new_index_tuples.append((headsign_short, tid_short))
    multi_route_schedule.index = pd.MultiIndex.from_tuples(new_index_tuples, names=['trip_headsign', multi_route_schedule.index.names[1]])

    # prepare display names for stops (disambiguate duplicates)
    # Direct clean dictionary extraction
    stop_map = feed.stops.set_index('stop_id')['stop_name'].to_dict() if feed.stops is not None else {}

    display_columns = []
    seen = set()
    for col in multi_route_schedule.columns:
        disp = stop_map.get(col, col)
        if disp in seen:
            disp = f"{disp} ({col})"
        seen.add(disp)
        display_columns.append(disp)

    # normalize times
    def normalize_time_val(v):
        if pd.isna(v):
            return v
        try:
            td = pd.to_timedelta(v)
        except Exception:
            return v
        total_seconds = td.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        hours_mod = hours % 24
        return f"{hours_mod:02d}:{minutes:02d}"

    # Modern pandas drops applymap in favor of map for element-wise adjustments
    multi_route_schedule = multi_route_schedule.map(normalize_time_val)

        # Precompute first and last served stop sequence indices using position matching
    trip_first_last = {}
    for col_idx, col_key in enumerate(ordered_columns):
        series = timetable_matrix[col_key]
        mask = [not (pd.isna(series.iat[i]) or str(series.iat[i]).strip() == "") for i in range(len(ordered_stops))]
        if any(mask):
            first = next(i for i, v in enumerate(mask) if v)
            last = len(mask) - 1 - next(i for i, v in enumerate(reversed(mask)) if v)
            # Use the exact column position index as the lookup key for safety
            trip_first_last[col_idx] = (first, last)

    # Fill routing arrows using high-speed direct positional tracking (.iat)
    multi_route_schedule.reset_index(inplace=True)
    cols = list(multi_route_schedule.columns)
    n_display = len(display_columns)
    stop_col_offset = len(cols) - n_display
    
    for pos in range(len(multi_route_schedule)):
        if pos not in trip_first_last: 
            continue
        first, last = trip_first_last[pos]
        
        for i in range(max(0, first), min(n_display - 1, last) + 1):
            cpos = stop_col_offset + i
            cur = multi_route_schedule.iat[pos, cpos]
            
            if pd.isna(cur) or (isinstance(cur, str) and cur.strip() == ""):
                multi_route_schedule.iat[pos, cpos] = '→'


    # apply display column names and return the final DataFrame
    if n_display:
        prefix = list(multi_route_schedule.columns[: len(cols) - n_display])
        multi_route_schedule.columns = prefix + display_columns

    return multi_route_schedule


def generate_both_directions(
    feed: gk.Feed,
    target_date: str,
    target_pairs: List[Tuple[str, int]],
    dup_map_path: Optional[str] = 'duplicate_stn.json',
    out_excel: str = 'combined_routes_schedule.xlsx',
    verbose: bool = False,
):
    """Generate forward and reverse timetables and write them into a single Excel workbook.

    Sheets: 'forward' and 'reverse'
    """
    df_fwd = generate_combined_schedule(feed, target_date, target_pairs, dup_map_path, reverse=False, verbose=verbose)
    df_rev = generate_combined_schedule(feed, target_date, target_pairs, dup_map_path, reverse=True, verbose=verbose)

    # Write both sheets into a single workbook
    with pd.ExcelWriter(out_excel, engine='xlsxwriter') as writer:
        df_fwd.to_excel(writer, sheet_name='forward', index=False)
        df_rev.to_excel(writer, sheet_name='reverse', index=False)
        # apply simple formatting to both sheets
        workbook = writer.book
        vf = workbook.add_format({'rotation': 90})
        try:
            writer.sheets['forward'].set_row(0, None, vf)
        except Exception:
            pass
        try:
            writer.sheets['reverse'].set_row(0, None, vf)
        except Exception:
            pass


if __name__ == '__main__':
    # Example usage (adjust target date and pairs as needed)
    TARGET_DATE = '20261025'
    TARGET_PAIRS = [
        ('09261126-LW', 1),
        ('09261126-18', 0),
        ('09261126-12', 0),
        # ('09261126-25', 1),
    ]
    feed = gk.read_feed('GO-GTFS (3).zip', dist_units='km')
    generate_both_directions(feed, TARGET_DATE, TARGET_PAIRS
                            #  , verbose=True
                             )
