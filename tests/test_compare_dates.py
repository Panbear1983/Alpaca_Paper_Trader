"""Compare wallets panel (Shift+W): each daily equity point is labelled with
the session whose close it carries. Alpaca stamps that close at 00:00 UTC of
the NEXT day, so the label must come from New York time, not the UTC date.
Stamps and equities are real High Risk points read on 2026-10-14."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tui  # noqa: E402


def _utc(s: str) -> str:
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).isoformat()


def _labels(hist, fmt="%m/%d"):
    return tui.CompareScreen._hist_labels(None, hist, fmt)


DAILY = [
    (_utc("2026-10-09T00:00"), 70844.27),   # Thu 10-08 close (16:00 ET intraday: 70,863)
    (_utc("2026-10-10T00:00"), 72919.97),   # Fri 10-09 close, stamped on a Saturday
    (_utc("2026-10-13T00:00"), 71206.91),   # Mon 10-12 close (16:00 ET intraday: 71,207)
]


def test_daily_points_carry_their_session_date():
    ds, ys = _labels(DAILY)
    assert ds == ["10/08", "10/09", "10/12"]
    assert ys == [70844.27, 72919.97, 71206.91]


def test_still_right_after_daylight_saving_ends():
    """00:00 UTC Tue 11-03 is 19:00 EST Mon 11-02: still Monday's close."""
    assert _labels([(_utc("2026-11-03T00:00"), 1.0)])[0] == ["11/02"]


def test_live_point_does_not_share_a_date_with_yesterdays_close():
    """In market hours the panel appends live equity; under the old UTC rule
    yesterday's close and today's live point were both labelled 10/14."""
    hist = [(_utc("2026-10-14T00:00"), 70995.63),     # Tue 10-13 close
            (_utc("2026-10-14T18:00"), 69900.00)]     # live, Wed 14:00 ET
    assert _labels(hist)[0] == ["10/13", "10/14"]


def test_intraday_windows_stay_in_new_york_time():
    assert _labels([(_utc("2026-10-14T13:30"), 1.0)], "%m/%d %H:%M")[0] == ["10/14 09:30"]


def test_unparseable_points_are_skipped():
    assert _labels([("not a time", 1.0), DAILY[0]]) == (["10/08"], [70844.27])
