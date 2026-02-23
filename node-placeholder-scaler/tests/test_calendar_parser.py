"""
Tests for scaler/calendar_parser.py
"""

import datetime
import zoneinfo
from unittest.mock import MagicMock, patch

import pytest
from ical.calendar_stream import IcsCalendarStream
from scaler.calendar_parser import (
    _event_repr,
    _get_cal_tz,
    get_calendar,
    get_events,
)

UTC = zoneinfo.ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# ICS fixtures
# ---------------------------------------------------------------------------


def _vcal(*events):
    """Wrap VEVENT blocks in a minimal VCALENDAR."""
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Test//Test//EN\n"
        + "\n".join(events)
        + "\nEND:VCALENDAR\n"
    )


def _vevent(uid, dtstart, dtend, summary="Test Event", description="pool-a: 1"):
    return (
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"DTSTART:{dtstart}\n"
        f"DTEND:{dtend}\n"
        f"SUMMARY:{summary}\n"
        f"DESCRIPTION:{description}\n"
        "END:VEVENT"
    )


# Single event 2023-04-27 17:00–18:00 UTC
ICS_ONE_EVENT = _vcal(
    _vevent(
        "ev1@test", "20230427T170000Z", "20230427T180000Z", "Event One", "pool-a: 3"
    )
)

# Two simultaneous events 2023-04-27 17:00–18:00 UTC
ICS_TWO_EVENTS = _vcal(
    _vevent(
        "ev1@test", "20230427T170000Z", "20230427T180000Z", "Event One", "pool-a: 3"
    ),
    _vevent(
        "ev2@test", "20230427T170000Z", "20230427T180000Z", "Event Two", "pool-b: 5"
    ),
)

# Event with HTML tags in the description
ICS_HTML_DESC = _vcal(
    _vevent(
        "ev-html@test",
        "20230427T170000Z",
        "20230427T180000Z",
        "HTML Event",
        "<b>pool-a</b>: <i>3</i>",
    )
)

# All-day event spanning 2023-04-27 through 2023-04-28 (2 days)
ICS_ALL_DAY = _vcal(
    "BEGIN:VEVENT\n"
    "UID:ev-allday@test\n"
    "DTSTART;VALUE=DATE:20230427\n"
    "DTEND;VALUE=DATE:20230429\n"
    "SUMMARY:All Day Event\n"
    "DESCRIPTION:pool-c: 2\n"
    "END:VEVENT"
)

# No events at all
ICS_EMPTY = _vcal()


# ---------------------------------------------------------------------------
# _event_repr
# ---------------------------------------------------------------------------


class TestEventRepr:
    def _make_event(self, summary, start, end, duration_days):
        ev = MagicMock()
        ev.summary = summary
        ev.start = start
        ev.end = end
        ev.computed_duration.days = duration_days
        return ev

    def test_all_day_event_shows_summary_and_date(self):
        """Multi-day (duration >= 1 day) events show summary + str(start)."""
        ev = self._make_event(
            "Conference",
            datetime.date(2023, 4, 27),
            datetime.date(2023, 4, 29),
            duration_days=2,
        )
        result = _event_repr(ev)
        assert "Conference" in result
        assert "2023-04-27" in result

    def test_all_day_event_single_day(self):
        ev = self._make_event(
            "Holiday",
            datetime.date(2023, 6, 15),
            datetime.date(2023, 6, 16),
            duration_days=1,
        )
        result = _event_repr(ev)
        assert "Holiday" in result
        assert "2023-06-15" in result

    def test_same_day_intraday_event(self):
        """Intraday event on a single day: shows start datetime and end time only."""
        start = datetime.datetime(2023, 4, 27, 17, 0, tzinfo=UTC)
        end = datetime.datetime(2023, 4, 27, 18, 0, tzinfo=UTC)
        ev = self._make_event("Seminar", start, end, duration_days=0)
        result = _event_repr(ev)
        assert "Seminar" in result
        assert "2023-04-27" in result
        assert "17:00" in result
        assert "18:00" in result

    def test_same_day_event_end_has_no_date_prefix(self):
        """End time for a same-day event uses short format (no date)."""
        start = datetime.datetime(2023, 4, 27, 9, 0, tzinfo=UTC)
        end = datetime.datetime(2023, 4, 27, 10, 30, tzinfo=UTC)
        ev = self._make_event("Morning Stand-up", start, end, duration_days=0)
        result = _event_repr(ev)
        # The start side must include the date; count occurrences of the date
        assert result.count("2023-04-27") == 1

    def test_overnight_event_shows_both_dates(self):
        """An event spanning midnight (< 1 day duration) shows full end datetime."""
        start = datetime.datetime(2023, 4, 27, 23, 0, tzinfo=UTC)
        end = datetime.datetime(2023, 4, 28, 1, 0, tzinfo=UTC)
        ev = self._make_event("Late Night", start, end, duration_days=0)
        result = _event_repr(ev)
        assert "2023-04-27" in result
        assert "2023-04-28" in result

    def test_includes_timezone_in_output(self):
        la = zoneinfo.ZoneInfo("America/Los_Angeles")
        start = datetime.datetime(2023, 4, 27, 17, 0, tzinfo=la)
        end = datetime.datetime(2023, 4, 27, 18, 0, tzinfo=la)
        ev = self._make_event("LA Event", start, end, duration_days=0)
        result = _event_repr(ev)
        # strftime %Z returns "PDT" or "PST" depending on DST
        assert "PDT" in result or "PST" in result


# ---------------------------------------------------------------------------
# _get_cal_tz
# ---------------------------------------------------------------------------


class TestGetCalTz:
    def _cal(self, tz_ids):
        cal = MagicMock()
        tzs = []
        for tz_id in tz_ids:
            tz = MagicMock()
            tz.tz_id = tz_id
            tzs.append(tz)
        cal.timezones = tzs
        return cal

    def test_single_timezone_returned(self):
        cal = self._cal(["America/Los_Angeles"])
        result = _get_cal_tz(cal)
        assert result == zoneinfo.ZoneInfo("America/Los_Angeles")

    def test_no_timezones_returns_utc(self):
        cal = self._cal([])
        result = _get_cal_tz(cal)
        assert result == zoneinfo.ZoneInfo("UTC")

    def test_multiple_timezones_returns_utc(self):
        cal = self._cal(["America/Los_Angeles", "America/New_York"])
        result = _get_cal_tz(cal)
        assert result == zoneinfo.ZoneInfo("UTC")

    def test_single_utc_timezone(self):
        cal = self._cal(["UTC"])
        result = _get_cal_tz(cal)
        assert result == zoneinfo.ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# get_calendar
# ---------------------------------------------------------------------------


class TestGetCalendar:
    def test_file_url_returns_calendar(self, tmp_path):
        ics_file = tmp_path / "cal.ics"
        ics_file.write_text(ICS_ONE_EVENT)
        result = get_calendar(f"file://{ics_file}")
        assert result is not None

    def test_bare_path_returns_calendar(self, tmp_path):
        ics_file = tmp_path / "cal.ics"
        ics_file.write_text(ICS_ONE_EVENT)
        result = get_calendar(str(ics_file))
        assert result is not None

    def test_file_url_and_bare_path_equivalent(self, tmp_path):
        ics_file = tmp_path / "cal.ics"
        ics_file.write_text(ICS_ONE_EVENT)
        cal_file = get_calendar(f"file://{ics_file}")
        cal_bare = get_calendar(str(ics_file))
        # Both should return a calendar with the same number of events
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        assert len(list(cal_file.timeline.at_instant(t))) == len(
            list(cal_bare.timeline.at_instant(t))
        )

    @patch("scaler.calendar_parser.requests.get")
    def test_http_200_returns_calendar(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ICS_ONE_EVENT
        mock_get.return_value = resp
        result = get_calendar("https://example.com/cal.ics")
        assert result is not None

    @patch("scaler.calendar_parser.requests.get")
    def test_http_500_returns_none(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        mock_get.return_value = resp
        result = get_calendar("https://example.com/cal.ics")
        assert result is None

    @patch("scaler.calendar_parser.requests.get")
    def test_http_error_status_raises(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = resp
        with pytest.raises(Exception):
            get_calendar("https://example.com/cal.ics")

    @patch("scaler.calendar_parser.requests.get")
    def test_http_url_passed_to_requests(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ICS_ONE_EVENT
        mock_get.return_value = resp
        url = "https://example.com/calendar.ics"
        get_calendar(url)
        mock_get.assert_called_once_with(url)

    def test_file_url_with_events_parseable(self, tmp_path):
        """Confirm that the returned calendar has queryable events."""
        ics_file = tmp_path / "cal.ics"
        ics_file.write_text(ICS_TWO_EVENTS)
        cal = get_calendar(f"file://{ics_file}")
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = list(cal.timeline.at_instant(t))
        assert len(events) == 2


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------


class TestGetEvents:
    def _cal(self, ics_text):
        return IcsCalendarStream.calendar_from_ics(ics_text)

    def test_event_at_matching_time_returned(self):
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 1

    def test_event_summary_preserved(self):
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert events[0].summary == "Event One"

    def test_no_events_outside_time_range(self):
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 19, 0, tzinfo=UTC)  # after event ends
        events = get_events(cal, time=t)
        assert len(events) == 0

    def test_before_event_start_returns_empty(self):
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 16, 59, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 0

    def test_two_simultaneous_events_both_returned(self):
        cal = self._cal(ICS_TWO_EVENTS)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 2

    def test_empty_calendar_returns_empty_list(self):
        cal = self._cal(ICS_EMPTY)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert events == []

    def test_html_stripped_from_description(self):
        cal = self._cal(ICS_HTML_DESC)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 1
        assert "<b>" not in events[0].description
        assert "<i>" not in events[0].description
        assert "pool-a" in events[0].description

    def test_html_stripped_leaves_text_content(self):
        cal = self._cal(ICS_HTML_DESC)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        # <b>pool-a</b>: <i>3</i>  →  pool-a: 3
        assert events[0].description == "pool-a: 3"

    def test_plain_description_unchanged(self):
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert events[0].description == "pool-a: 3"

    def test_time_none_returns_list(self):
        """When time=None, get_events should return a list (uses current time)."""
        cal = self._cal(ICS_ONE_EVENT)
        result = get_events(cal, time=None)
        assert isinstance(result, list)

    def test_all_day_event_returned_at_noon(self):
        cal = self._cal(ICS_ALL_DAY)
        # All-day events spanning 2023-04-27 through 2023-04-28
        t = datetime.datetime(2023, 4, 27, 12, 0, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 1
        assert events[0].summary == "All Day Event"

    def test_all_day_event_not_returned_after_end(self):
        cal = self._cal(ICS_ALL_DAY)
        t = datetime.datetime(2023, 4, 29, 12, 0, tzinfo=UTC)
        events = get_events(cal, time=t)
        assert len(events) == 0

    def test_returns_list_not_generator(self):
        """get_events must return a list, not a generator or iterator."""
        cal = self._cal(ICS_ONE_EVENT)
        t = datetime.datetime(2023, 4, 27, 17, 30, tzinfo=UTC)
        result = get_events(cal, time=t)
        assert isinstance(result, list)
