"""Deterministic timetable ordering using stop sequences and service-day times."""

from collections import Counter, defaultdict
from itertools import groupby
import heapq
import math

import pandas as pd


def toposort_from_edges(nodes, edges, coords=None, priority_node=None, *, key=None):
    """Return a stable topological order, or None when precedence is cyclic.

    ``coords`` remains accepted for API compatibility; geography cannot determine
    travel order around a lake or across branches. A source/sink priority node
    goes at the respective endpoint without overriding any real stop sequence.
    """
    nodes = set(nodes)
    adjacency = defaultdict(set)
    indegree = dict.fromkeys(nodes, 0)
    for start, end in edges:
        nodes.update((start, end))
        indegree.setdefault(start, 0)
        indegree.setdefault(end, 0)
        if end not in adjacency[start]:
            adjacency[start].add(end)
            indegree[end] += 1
    priority_source = priority_node in nodes and indegree[priority_node] == 0
    priority_sink = priority_node in nodes and not adjacency[priority_node]

    def rank(node):
        endpoint = 0
        if node == priority_node:
            endpoint = -1 if priority_source else (1 if priority_sink else 0)
        return endpoint, key(node) if key else str(node), str(node)

    serial = {node: i for i, node in enumerate(sorted(nodes, key=str))}
    ready = [(rank(n), serial[n], n) for n in nodes if not indegree[n]]
    heapq.heapify(ready)
    result = []
    while ready:
        _, _, node = heapq.heappop(ready)
        result.append(node)
        for child in sorted(adjacency[node], key=str):
            indegree[child] -= 1
            if not indegree[child]:
                heapq.heappush(ready, (rank(child), serial[child], child))
    return result if len(result) == len(nodes) else None


def order_stops(data, route_order, priority_node=None):
    """Use the first active route's longest graph path as the reference corridor.

    All trips contribute hard precedence edges. Branches are placed immediately
    after their latest upstream corridor stop (or before their first downstream
    corridor stop). Every observed trip retains its travel order.
    """
    sequences = []
    edges = set()
    for (route, trip), group in data.groupby(['route_id', 'trip_id'], sort=True):
        sequence = tuple(group.sort_values('stop_sequence', kind='stable').stop_id)
        sequences.append((route, sequence))
        edges.update((a, b) for a, b in zip(sequence, sequence[1:]) if a != b)
    nodes = set(data.stop_id)
    baseline = toposort_from_edges(nodes, edges)
    if baseline is None:
        conflicts = sorted({a for a, _ in edges})
        raise ValueError(
            'Conflicting stop sequences: a single station-column order cannot '
            'represent these trips. Check route directions, duplicate-stop '
            'mapping, or select a compatible stop_ids subset. '
            f'Stops involved: {", ".join(conflicts)}'
        )
    spine = ()
    for route in route_order:
        patterns = Counter(seq for rid, seq in sequences if rid == route)
        if patterns:
            # Combine express and local patterns: the longest *single trip*
            # may be a short local, omitting a distant express-only terminal.
            route_nodes = {stop for pattern in patterns for stop in pattern}
            route_edges = Counter()
            for pattern, frequency in patterns.items():
                for first, second in zip(pattern, pattern[1:]):
                    if first != second:
                        route_edges[first, second] += frequency
            route_adj = defaultdict(set)
            for first, second in route_edges:
                route_adj[first].add(second)
            route_topo = toposort_from_edges(route_nodes, route_edges)
            length = {}
            for node in reversed(route_topo):
                length[node] = 1 + max((length[child] for child in route_adj[node]), default=0)
            current = min(route_nodes, key=lambda n: (-length[n], n))
            path = [current]
            while route_adj[current]:
                current = min(route_adj[current], key=lambda n: (-length[n], -route_edges[current, n], n))
                path.append(current)
            spine = tuple(path)
            break
    positions = {stop: i for i, stop in enumerate(spine)}
    adjacency = defaultdict(set)
    for start, end in edges:
        adjacency[start].add(end)
    upstream = {n: positions.get(n, -1) for n in nodes}
    downstream = {n: positions.get(n, len(spine)) for n in nodes}
    for node in baseline:
        for child in adjacency[node]:
            upstream[child] = max(upstream[child], upstream[node])
    for node in reversed(baseline):
        for child in adjacency[node]:
            downstream[node] = min(downstream[node], downstream[child])

    def rank(node):
        if node in positions:
            return positions[node]
        if upstream[node] >= 0:
            return upstream[node] + 0.5
        if downstream[node] < len(spine):
            return downstream[node] - 0.5
        return len(spine) + 1

    return toposort_from_edges(nodes, edges, priority_node=priority_node, key=rank)


def validate_stop_order(data, stops):
    """Reject a manual column order that would make a trip run backwards."""
    positions = {stop: i for i, stop in enumerate(stops)}
    for trip, group in data.groupby('trip_id', sort=True):
        sequence = [s for s in group.sort_values('stop_sequence').stop_id if s in positions]
        for first, second in zip(sequence, sequence[1:]):
            if positions[first] > positions[second]:
                raise ValueError(
                    f'stop_ids contradict trip {trip}: {first} must precede {second}. '
                    'Use canonical stop IDs in forward travel order.'
                )


def time_seconds(value):
    """Parse GTFS HH:MM:SS without wrapping hours at midnight."""
    if pd.isna(value) or value == '':
        return float('nan')
    parts = str(value).split(':')
    try:
        hour, minute, second = map(int, parts)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid GTFS time: {value!r}') from None
    if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError(f'Invalid GTFS time: {value!r}')
    return hour * 3600 + minute * 60 + second


def format_time(value):
    """Keep the following service day visible, e.g. 25:10:00 -> 01:10+1."""
    seconds = time_seconds(value)
    if math.isnan(seconds):
        return ''
    hour, remainder = divmod(int(seconds), 3600)
    day, hour = divmod(hour, 24)
    return f'{hour:02d}:{remainder // 60:02d}' + (f'+{day}' if day else '')


def order_trips(times, sort_stop_id=None, *, start_times=None, verbose=False):
    """Order trips by shared-stop times, giving a reference stop precedence.

    The default reference is the busiest stop; ties use displayed stop order.
    Later stations can break equal-time ties, but cannot reverse ordering at
    the reference stop. Disconnected trips use first departure, then stable IDs.
    ``times`` has stops on rows, (route_id, trip_id) on columns, seconds in cells.
    """
    nodes = list(times.columns)
    if not nodes:
        return [], None
    coverage = times.notna().sum(axis=1)
    stations = sorted(times.index, key=lambda s: (-coverage[s], times.index.get_loc(s)))
    if sort_stop_id is not None:
        if sort_stop_id not in times.index or not coverage[sort_stop_id]:
            raise ValueError(f'Trip sorting stop {sort_stop_id!r} has no timed calls.')
        stations.remove(sort_stop_id)
        stations.insert(0, sort_stop_id)
    anchor = stations[0] if stations and coverage[stations[0]] else None
    adjacency = defaultdict(set)
    edges = []
    skipped = 0

    def reaches(start, target):
        pending, visited = [start], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node not in visited:
                visited.add(node)
                pending.extend(adjacency[node])
        return False

    for stop in stations:
        entries = sorted(
            ((time, node) for node, time in times.loc[stop].items() if pd.notna(time)),
            key=lambda entry: (entry[0], entry[1]),
        )
        groups = [[node for _, node in group] for _, group in groupby(entries, key=lambda x: x[0])]
        # Equal departures are not an ordering constraint. Connect *all* members
        # of consecutive time groups so tie-breaking never loses chronology.
        for earlier, later in zip(groups, groups[1:]):
            for first in earlier:
                for second in later:
                    if second in adjacency[first]:
                        continue
                    if reaches(second, first):
                        skipped += 1
                        continue
                    adjacency[first].add(second)
                    edges.append((first, second))
    if start_times is None:
        start_times = times.min(axis=0)

    def rank(node):
        time = start_times[node]
        return (float(time) if pd.notna(time) else float('inf'), node)

    ordered = toposort_from_edges(nodes, edges, key=rank)
    if verbose:
        print(f'[DEBUG] trip sorting stop={anchor}; skipped {skipped} overtaking constraints')
    return ordered, anchor
