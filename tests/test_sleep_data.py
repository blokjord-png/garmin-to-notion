import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).parents[1] / "src" / "workflows" / "sleep-data.py"
SPEC = importlib.util.spec_from_file_location("sleep_data_workflow", MODULE_PATH)
sleep_data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sleep_data)


def sample_daily_sleep(score=87):
    return {
        "calendarDate": "2026-07-17",
        "deepSleepSeconds": 3600,
        "lightSleepSeconds": 14400,
        "remSleepSeconds": 5400,
        "awakeSleepSeconds": 600,
        "sleepStartTimestampGMT": 1784239200000,
        "sleepEndTimestampGMT": 1784262600000,
        "sleepScores": {"overall": {"value": score}},
    }


class SleepScoreTests(unittest.TestCase):
    def test_extracts_overall_sleep_score(self):
        self.assertEqual(sleep_data.get_sleep_score(sample_daily_sleep()), 87)

    def test_missing_or_invalid_sleep_score_is_ignored(self):
        self.assertIsNone(sleep_data.get_sleep_score({}))
        self.assertIsNone(sleep_data.get_sleep_score(sample_daily_sleep(score=101)))
        self.assertIsNone(sleep_data.get_sleep_score(sample_daily_sleep(score=True)))

    def test_new_sleep_entry_includes_score(self):
        client = MagicMock()
        payload = {
            "dailySleepDTO": sample_daily_sleep(),
            "restingHeartRate": 44,
        }

        sleep_data.create_sleep_data(client, "database-id", payload)

        properties = client.pages.create.call_args.kwargs["properties"]
        self.assertEqual(properties["Sleep Score"], {"number": 87})

    def test_existing_sleep_entry_is_updated_without_other_properties(self):
        client = MagicMock()
        existing_page = {
            "id": "page-id",
            "properties": {"Sleep Score": {"number": None}},
        }

        changed = sleep_data.update_sleep_score(
            client,
            existing_page,
            sample_daily_sleep(),
            "2026-07-17",
        )

        self.assertTrue(changed)
        client.pages.update.assert_called_once_with(
            page_id="page-id",
            properties={"Sleep Score": {"number": 87}},
        )

    def test_existing_matching_score_is_not_rewritten(self):
        client = MagicMock()
        existing_page = {
            "id": "page-id",
            "properties": {"Sleep Score": {"number": 87}},
        }

        changed = sleep_data.update_sleep_score(
            client,
            existing_page,
            sample_daily_sleep(),
            "2026-07-17",
        )

        self.assertFalse(changed)
        client.pages.update.assert_not_called()

    def test_sleep_backfill_window_is_configurable(self):
        with patch.dict(os.environ, {"GARMIN_SLEEP_DAYS_BACK": "500"}):
            self.assertEqual(sleep_data.get_sleep_days_back(), 500)


if __name__ == "__main__":
    unittest.main()
