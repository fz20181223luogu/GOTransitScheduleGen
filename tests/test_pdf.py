import pandas as pd
from pypdf import PdfReader

from schedule_pdf import export_schedule_pdf


def test_wide_long_timetable_repeats_headers_and_all_trip_rows(tmp_path):
    stops = [f'Station {i:02}' for i in range(35)]
    frame = pd.DataFrame([
        ['R', f'T{trip:03}'] + ['08:00'] * len(stops) for trip in range(75)
    ], columns=['trip_headsign', 'trip_id'] + stops)
    path = tmp_path / 'wide.pdf'
    export_schedule_pdf({'forward': frame}, path, target_date='20261029')
    pages = PdfReader(path).pages
    assert len(pages) == 6  # Three row groups, two station panels each.
    text = [page.extract_text() for page in pages]
    assert all('Route' in t and 'Trip' in t and 'Station panel' in t for t in text)
    for stop in stops:
        assert sum(stop in t for t in text) == 3
    for trip in range(75):
        assert sum(f'T{trip:03}' in t for t in text) == 2
    assert '6 / 6' in text[-1]


def test_empty_pdf_explains_missing_service(tmp_path):
    path = tmp_path / 'empty.pdf'
    empty = pd.DataFrame(columns=['trip_headsign', 'trip_id'])
    export_schedule_pdf({'forward': empty, 'reverse': empty}, path, target_date='20261029')
    pages = PdfReader(path).pages
    assert len(pages) == 2
    assert all('No active trips' in page.extract_text() for page in pages)
