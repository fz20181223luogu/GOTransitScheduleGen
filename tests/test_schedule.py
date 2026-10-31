import json
from zipfile import ZipFile

import gtfs_kit as gk
import pandas as pd
import pytest

from ScheduleGen import generate_both_directions, generate_combined_schedule


def make_feed(trips=None):
    if trips is None:
        trips = [
            ('LW', 'day-LW-1', 1, [('NI', '08:00:00'), ('BU', '09:00:00'), ('UN', '10:00:00')]),
            ('LW', 'day-LW-2', 0, [('UN', '08:00:00'), ('BU', '09:00:00'), ('NI', '10:00:00')]),
            ('12', 'day-12-3', 0, [('NI', '08:30:00'), ('BU', '09:30:00'), ('DD', '09:45:00')]),
            ('12', 'day-12-4', 1, [('DD', '08:15:00'), ('BU', '08:30:00'), ('NI', '09:30:00')]),
        ]
    ids = sorted({'NI', 'BU', 'UN', 'DD', 'UN_BUS', 'INACTIVE'} | {s for _, _, _, calls in trips for s, _ in calls})
    names = {'NI': 'Niagara Falls GO', 'BU': 'Burlington GO', 'UN': 'Union Station GO', 'DD': 'Dundas / 407', 'UN_BUS': 'Union Bus'}
    return gk.Feed(
        dist_units='km',
        stops=pd.DataFrame([{'stop_id': s, 'stop_name': names.get(s, s)} for s in ids]),
        routes=pd.DataFrame([{'route_id': r, 'route_short_name': r, 'route_type': 2 if r == 'LW' else 3}
                             for r in sorted({r for r, _, _, _ in trips})]),
        trips=pd.DataFrame([{'route_id': r, 'trip_id': t, 'direction_id': d, 'service_id': 'daily', 'trip_headsign': r + ' - Destination'}
                            for r, t, d, _ in trips]),
        stop_times=pd.DataFrame([{'trip_id': t, 'stop_id': s, 'stop_sequence': i, 'arrival_time': time, 'departure_time': time}
                                 for _, t, _, calls in trips for i, (s, time) in enumerate(calls)]),
        calendar_dates=pd.DataFrame([{'service_id': 'daily', 'date': '20261029', 'exception_type': 1}]),
    )


def generate(feed=None, **kwargs):
    return generate_combined_schedule(feed or make_feed(), '20261029', [('LW', 1), ('12', 0)], dup_map_path=None, **kwargs)


def test_real_gtfs_api_combines_routes_and_preserves_union_endpoint():
    frame = generate()
    assert frame.attrs['stop_ids'] == ['NI', 'BU', 'DD', 'UN']
    assert set(frame.trip_headsign) == {'LW', '12'}
    assert len(frame) == 2


def test_manual_stops_define_exact_order_and_reverse_automatically():
    selected = ['NI', 'BU', 'DD', 'UN']
    assert generate(stop_ids=selected).attrs['stop_ids'] == selected
    assert generate(stop_ids=selected, reverse=True).attrs['stop_ids'] == list(reversed(selected))


def test_manual_selection_omits_unserved_trips_and_keeps_known_inactive_column():
    frame = generate(stop_ids=['UN', 'INACTIVE'])
    assert frame.trip_headsign.tolist() == ['LW']
    assert frame['INACTIVE'].tolist() == ['']


def test_inactive_selected_stop_inside_trip_span_remains_blank():
    frame = generate(stop_ids=['NI', 'INACTIVE', 'UN'])
    assert set(frame['INACTIVE']) == {''}


def test_hidden_timepoint_can_control_sorting():
    frame = generate(stop_ids=['NI', 'BU'], trip_sort_stop='UN')
    assert frame.attrs['trip_sort_stop'] == 'UN'
    assert 'Union Station GO' not in frame.columns


@pytest.mark.parametrize('selected, message', [
    ([], 'nonempty'), (['NI', 'NI'], 'duplicate'), (['MISSING'], 'Unknown'), (['UN', 'NI'], 'contradict'),
])
def test_invalid_stop_lists_fail_clearly(selected, message):
    with pytest.raises(ValueError, match=message):
        generate(stop_ids=selected)


def test_empty_direction_does_not_crash():
    feed = make_feed([('LW', 'only', 1, [('NI', '08:00:00'), ('UN', '09:00:00')])])
    frame = generate_combined_schedule(feed, '20261029', [('LW', 0)], dup_map_path=None)
    assert frame.empty
    assert frame.columns.tolist() == ['trip_headsign', 'trip_id']


def test_date_without_service_produces_empty_table():
    frame = generate_combined_schedule(make_feed(), '20261030', [('LW', 1)], dup_map_path=None, stop_ids=['NI', 'UN'])
    assert frame.empty
    assert frame.columns.tolist() == ['trip_headsign', 'trip_id', 'Niagara Falls GO', 'Union Station GO']


def test_feed_errors_are_not_hidden():
    feed = make_feed()
    feed.stop_times = feed.stop_times.drop(columns='departure_time')
    with pytest.raises(KeyError):
        generate(feed)


def test_structured_duplicate_mapping_and_repeated_calls(tmp_path):
    feed = make_feed([('LW', 'loopless', 1, [('NI', '23:00:00'), ('UN_BUS', '25:00:00'), ('UN_BUS', '25:05:00')])])
    mapping = tmp_path / 'mapping.json'
    mapping.write_text(json.dumps([{'duplicate_stop_id': 'UN_BUS', 'canonical_stop_id': 'UN', 'priority': 100}]))
    frame = generate_combined_schedule(feed, '20261029', [('LW', 1)], dup_map_path=mapping)
    # A lower-priority duplicate occurrence is retained under its original ID.
    assert frame['Union Station GO'].iloc[0] == '01:00+1'
    assert frame['Union Bus'].iloc[0] == '01:05+1'


def test_repeated_adjacent_stop_times_remain_visible():
    feed = make_feed([('LW', 'repeat', 1, [('NI', '08:00:00'), ('UN', '09:00:00'), ('UN', '09:05:00')])])
    frame = generate_combined_schedule(feed, '20261029', [('LW', 1)], dup_map_path=None)
    assert frame['Union Station GO'].iloc[0] == '09:00 / 09:05'


def test_selected_subset_can_exclude_conflicting_stops():
    feed = make_feed([
        ('LW', 'one', 1, [('NI', '08:00:00'), ('BU', '09:00:00'), ('UN', '10:00:00')]),
        ('LW', 'two', 1, [('BU', '08:00:00'), ('NI', '09:00:00'), ('UN', '10:00:00')]),
    ])
    with pytest.raises(ValueError, match='Conflicting'):
        generate_combined_schedule(feed, '20261029', [('LW', 1)], dup_map_path=None)
    frame = generate_combined_schedule(feed, '20261029', [('LW', 1)], dup_map_path=None, stop_ids=['NI', 'UN'])
    assert len(frame) == 2


def test_route_short_names_resolve_feed_version_prefix():
    feed = make_feed()
    feed.routes.loc[feed.routes.route_id.eq('LW'), 'route_id'] = 'version-LW'
    feed.trips.loc[feed.trips.route_id.eq('LW'), 'route_id'] = 'version-LW'
    frame = generate(feed)
    assert any(route == 'version-LW' for route, _ in frame.attrs['trip_keys'])


def test_exports_include_both_directions(tmp_path):
    from pypdf import PdfReader
    excel, pdf = tmp_path / 'schedule.xlsx', tmp_path / 'schedule.pdf'
    result = generate_both_directions(make_feed(), '20261029', [('LW', 1), ('12', 0)],
                                      dup_map_path=None, out_excel=excel, out_pdf=pdf)
    assert set(result) == {'forward', 'reverse'}
    with ZipFile(excel) as workbook:
        xml = workbook.read('xl/workbook.xml').decode()
        assert all(name in xml for name in ['forward', 'reverse', 'notes'])
    pages = PdfReader(pdf).pages
    assert len(pages) == 2
    assert 'FORWARD' in pages[0].extract_text()
    assert 'REVERSE' in pages[1].extract_text()
    assert 'Union Station GO' in pages[0].extract_text()
