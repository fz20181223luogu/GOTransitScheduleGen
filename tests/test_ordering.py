import pandas as pd
import pytest

from schedule_ordering import format_time, order_stops, order_trips, time_seconds, toposort_from_edges


def network(patterns):
    return pd.DataFrame([
        {'route_id': route, 'trip_id': str(trip), 'stop_id': stop, 'stop_sequence': i}
        for trip, (route, sequence) in enumerate(patterns)
        for i, stop in enumerate(sequence)
    ])


def test_curved_corridor_and_branch_are_ordered_by_calls_not_distance():
    data = network([
        ('LW', ['NI', 'AL', 'BU', 'AP', 'EX', 'UN']),
        ('12', ['NI', 'MALL', 'BU', 'DUNDAS']),
        ('16', ['HAMILTON', 'UN']),
    ])
    ordered = order_stops(data, ['LW', '12', '16'], 'UN')
    assert ordered[-1] == 'UN'
    assert ordered.index('BU') < ordered.index('DUNDAS') < ordered.index('AP')
    for _, trip in data.groupby('trip_id'):
        positions = [ordered.index(s) for s in trip.stop_id]
        assert positions == sorted(positions)
    assert ordered == order_stops(data.sample(frac=1, random_state=7), ['LW', '12', '16'], 'UN')


def test_reverse_corridor_keeps_union_first_and_branch_next_to_burlington():
    data = network([
        ('LW', ['UN', 'EX', 'AP', 'BU', 'AL', 'NI']),
        ('12', ['DUNDAS', 'BU', 'MALL', 'NI']),
    ])
    ordered = order_stops(data, ['LW', '12'], 'UN')
    assert ordered[0] == 'UN'
    assert ordered.index('AP') < ordered.index('DUNDAS') < ordered.index('BU')


def test_express_only_terminal_is_kept_on_primary_corridor():
    data = network([
        ('LW', ['NI', 'ST', 'AL', 'BU', 'UN']),
        ('LW', ['AL', 'BU', 'AP', 'BO', 'OA', 'EX', 'UN']),
        ('12', ['NI', 'MALL', 'BU', 'DUNDAS']),
        ('11', ['BROCK', 'MALL']),
    ])
    ordered = order_stops(data, ['LW', '12', '11'], 'UN')
    assert ordered[0] == 'NI'
    assert ordered[-1] == 'UN'
    assert ordered.index('BU') < ordered.index('DUNDAS') < ordered.index('AP')


def test_cycle_is_reported_instead_of_silently_sorting_by_sequence_number():
    data = network([('a', ['A', 'B']), ('b', ['B', 'A'])])
    with pytest.raises(ValueError, match='Conflicting stop sequences'):
        order_stops(data, ['a', 'b'])


def test_priority_cannot_override_real_precedence():
    assert toposort_from_edges(['A', 'UN', 'B'], [('A', 'UN'), ('UN', 'B')], priority_node='UN') == ['A', 'UN', 'B']


def test_coordinates_do_not_affect_topology():
    assert toposort_from_edges(['A', 'B'], [('A', 'B')], coords={'A': (0, 0), 'B': (0, 180)}) == ['A', 'B']


def matrix(stops, calls):
    return pd.DataFrame(calls, index=stops).map(time_seconds)


def test_unconnected_trips_use_times_instead_of_route_ids():
    times = matrix(['A', 'B'], {('A', 'late'): [None, '11:00:00'], ('Z', 'early'): ['05:00:00', None]})
    assert order_trips(times)[0] == [('Z', 'early'), ('A', 'late')]


def test_overtaking_respects_selected_timepoint_and_keeps_both_trips():
    times = matrix(['A', 'UN'], {('R', 'slow'): ['08:00:00', '09:30:00'], ('R', 'fast'): ['08:10:00', '09:00:00']})
    assert order_trips(times, 'UN')[0] == [('R', 'fast'), ('R', 'slow')]
    assert order_trips(times, 'A')[0] == [('R', 'slow'), ('R', 'fast')]


def test_equal_departures_allow_downstream_tiebreaking():
    times = matrix(['A', 'B'], {('R', 'a'): ['08:00:00', '09:10:00'], ('R', 'z'): ['08:00:00', '09:00:00'], ('R', 'c'): ['08:05:00', '09:20:00']})
    assert order_trips(times, 'A')[0] == [('R', 'z'), ('R', 'a'), ('R', 'c')]


def test_after_midnight_is_sorted_on_service_day_and_marked():
    times = matrix(['A'], {('R', 'next'): ['25:05:00'], ('R', 'today'): ['23:55:00']})
    assert order_trips(times)[0] == [('R', 'today'), ('R', 'next')]
    assert format_time('25:05:00') == '01:05+1'
    assert format_time('48:01:00') == '00:01+2'
    assert format_time('8:03:00') == '08:03'


@pytest.mark.parametrize('invalid', ['08:99:00', 'bad', '-1:00:00', '10:00'])
def test_invalid_time_is_not_silently_dropped(invalid):
    with pytest.raises(ValueError, match='Invalid GTFS time'):
        time_seconds(invalid)
