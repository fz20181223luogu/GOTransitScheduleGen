"""Printable timetable grids with repeated station headers and trip identifiers."""

from datetime import datetime
from math import ceil
from pathlib import Path

import pandas as pd
import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase.pdfmetrics import registerFont, stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import simpleSplit


# Embed ReportLab's bundled Vera fonts so measurements and rendering agree
# on machines without Helvetica or a configured system font catalogue.
_FONT_DIR = Path(reportlab.__file__).parent / 'fonts'
registerFont(TTFont('ScheduleSans', str(_FONT_DIR / 'Vera.ttf')))
registerFont(TTFont('ScheduleSans-Bold', str(_FONT_DIR / 'VeraBd.ttf')))

INK = colors.HexColor('#182C24')
GREEN = colors.HexColor('#285C43')
PALE = colors.HexColor('#E9F0EA')
LINE = colors.HexColor('#C5D1C9')


def _fit_text(canvas, text, x, y, width, size=9, bold=False):
    font = 'ScheduleSans-Bold' if bold else 'ScheduleSans'
    measured = stringWidth(text, font, size)
    actual_size = min(size, size * width / measured) if measured else size
    canvas.setFont(font, actual_size)
    canvas.drawString(x, y, text)


def _arrow(canvas, x, y):
    """Draw an arrow without depending on a platform-specific Unicode font."""
    canvas.setStrokeColor(colors.HexColor('#768C7F'))
    canvas.setLineWidth(0.65)
    canvas.line(x - 5, y, x + 5, y)
    canvas.line(x + 2, y + 2, x + 5, y)
    canvas.line(x + 2, y - 2, x + 5, y)


def export_schedule_pdf(schedules, out_pdf, *, target_date, title='Combined transit timetable',
                        max_stops_per_panel=32, attribution=None):
    """Export landscape A3 pages with both horizontal and vertical pagination.

    Rows are never split. Every page repeats route/trip labels and the applicable
    station headings. Wide schedules use balanced column panels for the same
    group of trips. This is a schedule grid, not a journey/connection planner.
    """
    if not isinstance(max_stops_per_panel, int) or max_stops_per_panel < 1:
        raise ValueError('max_stops_per_panel must be a positive integer.')
    # Keep an eight-point clock legible even when called with a larger limit.
    max_stops_per_panel = min(max_stops_per_panel, 32)
    path = Path(out_pdf)
    path.parent.mkdir(parents=True, exist_ok=True)
    page_width, page_height = landscape(A3)
    margin, route_width, trip_width = 32, 42, 60
    header_height, row_height = 156, 16
    table_top = page_height - 104
    table_width = page_width - 2 * margin
    rows_per_page = int((table_top - header_height - 86) // row_height)
    plans = []
    for direction, frame in schedules.items():
        stops = list(frame.columns[2:])
        panel_count = max(1, ceil(len(stops) / max_stops_per_panel))
        panel_size = max(1, ceil(len(stops) / panel_count))
        panels = [stops[i:i + panel_size] for i in range(0, len(stops), panel_size)] or [[]]
        row_starts = range(0, len(frame), rows_per_page) if len(frame) else [0]
        for start in row_starts:
            for panel_index, panel in enumerate(panels):
                plans.append((direction, frame, panel, start, panel_index, len(panels)))
    canvas = Canvas(str(path), pagesize=(page_width, page_height), pageCompression=1)
    canvas.setTitle(title)
    canvas.setAuthor('GOTransitScheduleGen')
    date_label = datetime.strptime(target_date, '%Y%m%d').strftime('%A, %B %d, %Y')
    for page_number, (direction, frame, stops, start, panel_index, panel_count) in enumerate(plans, 1):
        canvas.setFillColor(GREEN)
        canvas.rect(0, page_height - 8, page_width, 8, stroke=0, fill=1)
        canvas.setFillColor(INK)
        _fit_text(canvas, title, margin, page_height - 39, table_width, size=21, bold=True)
        canvas.setFont('ScheduleSans', 10)
        canvas.drawString(margin, page_height - 59, f'Service date: {date_label}')
        canvas.setFont('ScheduleSans-Bold', 10)
        label = f'{direction.upper()} | Selected route directions' if direction == 'forward' else f'{direction.upper()} | Opposite route directions'
        canvas.drawString(margin, page_height - 82, label)
        section = f'Rows {start + 1}-{min(start + rows_per_page, len(frame))} of {len(frame)}'
        if frame.empty:
            section = 'No active trips'
        if panel_count > 1:
            section += f' | Station panel {panel_index + 1}/{panel_count}'
        canvas.setFont('ScheduleSans', 9)
        canvas.drawRightString(page_width - margin, page_height - 82, section)

        if not stops or frame.empty:
            canvas.setFillColor(PALE)
            canvas.rect(margin, table_top - 72, table_width, 72, stroke=0, fill=1)
            canvas.setFillColor(INK)
            canvas.setFont('ScheduleSans', 12)
            canvas.drawString(margin + 18, table_top - 40, 'No active trips for this date, direction and stop selection.')
        else:
            stop_width = (table_width - route_width - trip_width) / len(stops)
            widths = [route_width, trip_width] + [stop_width] * len(stops)
            boundaries = [margin]
            for width in widths:
                boundaries.append(boundaries[-1] + width)
            body_top = table_top - header_height
            subset = frame.iloc[start:start + rows_per_page]
            bottom = body_top - len(subset) * row_height
            canvas.setFillColor(PALE)
            canvas.rect(margin, body_top, table_width, header_height, stroke=0, fill=1)
            canvas.setFillColor(INK)
            for column, label in enumerate(['Route', 'Trip']):
                canvas.setFont('ScheduleSans-Bold', 9)
                canvas.drawString(boundaries[column] + 6, body_top + 12, label)
            for column, name in enumerate(stops, 2):
                # Wrap station names before rotation; the available vertical
                # extent is fixed and long headings never cross the title.
                lines = simpleSplit(str(name), 'ScheduleSans-Bold', 8, header_height - 20)
                canvas.saveState()
                canvas.translate(boundaries[column] + widths[column] / 2, body_top + 10)
                canvas.rotate(90)
                canvas.setFont('ScheduleSans-Bold', 8)
                for line, text in enumerate(lines):
                    canvas.drawString(0, (len(lines) - 1) * 4.5 - line * 9, text)
                canvas.restoreState()
            for offset, (_, row) in enumerate(subset.iterrows()):
                y = body_top - (offset + 1) * row_height
                if offset % 2 == 0:
                    canvas.setFillColor(colors.HexColor('#F4F7F4'))
                    canvas.rect(margin, y, table_width, row_height, stroke=0, fill=1)
                values = [row.iloc[0], row.iloc[1]] + [row[stop] for stop in stops]
                for column, value in enumerate(values):
                    if pd.isna(value) or value == '':
                        continue
                    if value == '→':
                        _arrow(canvas, boundaries[column] + widths[column] / 2, y + 8)
                        continue
                    canvas.setFillColor(INK)
                    text = str(value)
                    if column < 2:
                        _fit_text(canvas, text, boundaries[column] + 5, y + 4.5,
                                  widths[column] - 10, size=8, bold=(column == 0))
                    else:
                        # Multiple visits use stacked times, preserving legibility.
                        visits = text.split(' / ')
                        if len(visits) > 2:
                            raise ValueError('PDF supports at most two calls per trip/stop; select non-looping stops.')
                        size = 6 if len(visits) > 1 else 8
                        canvas.setFont('ScheduleSans', size)
                        for visit, time in enumerate(visits):
                            text_width = stringWidth(time, 'ScheduleSans', size)
                            fitted_size = min(size, size * (widths[column] - 4) / text_width)
                            text_width = stringWidth(time, 'ScheduleSans', fitted_size)
                            _fit_text(canvas, time,
                                      boundaries[column] + (widths[column] - text_width) / 2,
                                      y + (9 - 6 * visit if len(visits) > 1 else 4.5),
                                      widths[column] - 4, size=fitted_size)
            canvas.setStrokeColor(LINE)
            canvas.setLineWidth(0.4)
            for x in boundaries:
                canvas.line(x, bottom, x, table_top)
            for row in range(len(subset) + 1):
                y = body_top - row * row_height
                canvas.line(margin, y, page_width - margin, y)
            canvas.line(margin, table_top, page_width - margin, table_top)

        canvas.setFillColor(INK)
        canvas.setFont('ScheduleSans', 8)
        canvas.drawString(margin, 63, '24-hour agency-local time. +1 = following calendar day. Blank = outside displayed span or no service at this stop.')
        canvas.drawString(margin, 50, 'Arrow = no scheduled call in this column; it does not establish a physical path or a connection.')
        canvas.setFont('ScheduleSans', 7.5)
        canvas.drawString(margin, 34, 'Independently generated from GTFS. Verify service with the operator before travel.')
        canvas.drawRightString(page_width - margin, 34, f'{page_number} / {len(plans)}')
        if attribution:
            canvas.setFont('ScheduleSans', 6.5)
            for line, text in enumerate(simpleSplit(attribution, 'ScheduleSans', 6.5, table_width - 60)):
                canvas.drawString(margin, 22 - 8 * line, text)
        canvas.showPage()
    canvas.save()
