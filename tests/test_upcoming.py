import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Config
from scanner import scan_once
from state import State


class _Client:
    def __init__(self, record):
        self.record = record

    def ensure_login(self):
        return True

    def fetch_all_activities(self):
        return [self.record]

    def register(self, activity_id):
        return True, "ok"


class _Notifier:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _registered_record(days_from_now: int) -> dict:
    start = dt.datetime.now() + dt.timedelta(days=days_from_now)
    return {
        "infoId": "registered-1",
        "activityName": "已报名活动",
        "status": 1,
        "registerAndLimitPeople": "1/100",
        "startTime": int(start.timestamp() * 1000),
    }


class UpcomingNoticeTest(unittest.TestCase):
    def test_default_is_three_days(self) -> None:
        self.assertEqual(Config().upcoming_days, 3)

    def test_zero_disables_upcoming_notice(self) -> None:
        with TemporaryDirectory() as data_dir:
            cfg = Config()
            cfg.data_dir = data_dir
            cfg.upcoming_days = 0
            state = State(Path(data_dir) / "state.json")
            state.initialized = True
            state.seen_ids = ["registered-1"]
            state.save()
            notifier = _Notifier()

            result = scan_once(cfg, _Client(_registered_record(2)), notifier, state)

            self.assertEqual(result.upcoming, [])
            self.assertEqual(notifier.calls, [])
            self.assertEqual(state.last_upcoming_date, "")

    def test_three_days_still_notifies(self) -> None:
        with TemporaryDirectory() as data_dir:
            cfg = Config()
            cfg.data_dir = data_dir
            state = State(Path(data_dir) / "state.json")
            state.initialized = True
            state.seen_ids = ["registered-1"]
            state.save()
            notifier = _Notifier()

            result = scan_once(cfg, _Client(_registered_record(2)), notifier, state)

            self.assertEqual(len(result.upcoming), 1)
            self.assertTrue(notifier.calls)
            self.assertTrue(state.last_upcoming_date)


if __name__ == "__main__":
    unittest.main()
