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
        self.register_calls = []

    def ensure_login(self):
        return True

    def fetch_all_activities(self):
        return [self.record]

    def register(self, activity_id):
        self.register_calls.append(activity_id)
        return True, "ok"


class _Notifier:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _record(delta_seconds: int) -> dict:
    start = dt.datetime.now() + dt.timedelta(seconds=delta_seconds)
    return {
        "infoId": "future-1",
        "activityName": "未来开放活动",
        "status": 4,
        "registerAndLimitPeople": "0/100",
        "registerStartTime": int(start.timestamp() * 1000),
    }


class PendingRegistrationTest(unittest.TestCase):
    def test_seen_activity_registers_when_registration_opens(self) -> None:
        with TemporaryDirectory() as data_dir:
            cfg = Config()
            cfg.data_dir = data_dir
            state = State(Path(data_dir) / "state.json")
            state.initialized = True
            state.seen_ids = ["future-1"]
            state.save()

            client = _Client(_record(30))
            notifier = _Notifier()
            first = scan_once(cfg, client, notifier, state)

            self.assertEqual(client.register_calls, [])
            self.assertEqual(state.pending_registration_ids, ["future-1"])
            self.assertEqual(first.new_activities, [])

            client.record = _record(-30)
            second = scan_once(cfg, client, notifier, state)

            self.assertEqual(client.register_calls, ["future-1"])
            self.assertEqual(state.pending_registration_ids, [])
            self.assertEqual(second.reg_results, [("未来开放活动", "已自动报名")])

            scan_once(cfg, client, notifier, state)
            self.assertEqual(client.register_calls, ["future-1"])

    def test_pending_registration_does_not_fire_before_open(self) -> None:
        with TemporaryDirectory() as data_dir:
            cfg = Config()
            cfg.data_dir = data_dir
            state = State(Path(data_dir) / "state.json")
            state.initialized = True
            state.seen_ids = ["future-1"]
            state.save()

            client = _Client(_record(60))
            scan_once(cfg, client, _Notifier(), state)

            self.assertEqual(client.register_calls, [])
            self.assertIn("future-1", state.pending_registration_ids)


if __name__ == "__main__":
    unittest.main()
