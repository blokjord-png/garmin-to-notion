import importlib.util
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).parents[1] / "src" / "workflows" / "daily-steps.py"
SPEC = importlib.util.spec_from_file_location("daily_steps_workflow", MODULE_PATH)
daily_steps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_steps)


def garmin_day(calendar_date, total_steps=8000, step_goal=7500, total_distance=6120):
    return {
        "calendarDate": calendar_date,
        "totalSteps": total_steps,
        "stepGoal": step_goal,
        "totalDistance": total_distance,
    }


def notion_page(calendar_date, total_steps=8000, step_goal=7500, distance_km=6.12,
                activity_type="Walking"):
    return {
        "id": f"page-{calendar_date}",
        "properties": {
            "Date": {"date": {"start": calendar_date}},
            "Activity Type": {"title": [{"plain_text": activity_type,
                                         "text": {"content": activity_type}}]},
            "Total Steps": {"number": total_steps},
            "Step Goal": {"number": step_goal},
            "Total Distance (km)": {"number": distance_km},
        },
    }


class StepsNeedUpdateTests(unittest.TestCase):
    def test_identical_data_needs_no_update(self):
        self.assertFalse(daily_steps.steps_need_update(
            notion_page("2026-08-01"), garmin_day("2026-08-01")))

    def test_changed_steps_need_update(self):
        self.assertTrue(daily_steps.steps_need_update(
            notion_page("2026-08-01", total_steps=1), garmin_day("2026-08-01")))

    def test_distance_is_compared_in_kilometres(self):
        # 6120 m from Garmin == 6.12 km in Notion: no update needed.
        self.assertFalse(daily_steps.steps_need_update(
            notion_page("2026-08-01", distance_km=6.12),
            garmin_day("2026-08-01", total_distance=6120)))
        self.assertTrue(daily_steps.steps_need_update(
            notion_page("2026-08-01", distance_km=6.12),
            garmin_day("2026-08-01", total_distance=7000)))

    def test_missing_distance_counts_as_zero(self):
        self.assertFalse(daily_steps.steps_need_update(
            notion_page("2026-08-01", distance_km=0),
            garmin_day("2026-08-01", total_distance=None)))


class ExistingStepsQueryTests(unittest.TestCase):
    def test_paginates_and_keys_by_date(self):
        client = MagicMock()
        client.databases.query.side_effect = [
            {"results": [notion_page("2026-08-01")], "has_more": True, "next_cursor": "c1"},
            {"results": [notion_page("2026-08-02T00:00:00.000+02:00")], "has_more": False},
        ]

        existing = daily_steps.get_existing_daily_steps(client, "db", date(2026, 8, 1))

        self.assertEqual(set(existing), {"2026-08-01", "2026-08-02"})
        self.assertEqual(client.databases.query.call_count, 2)
        first_call = client.databases.query.call_args_list[0].kwargs
        second_call = client.databases.query.call_args_list[1].kwargs
        self.assertNotIn("start_cursor", first_call)
        self.assertEqual(second_call["start_cursor"], "c1")
        self.assertEqual(first_call["page_size"], 100)
        self.assertEqual(
            first_call["filter"]["and"][0],
            {"property": "Date", "date": {"on_or_after": "2026-08-01"}},
        )


class SyncTests(unittest.TestCase):
    def test_creates_updates_and_skips_unchanged(self):
        today = date(2026, 9, 2)
        garmin = MagicMock()
        garmin.get_daily_steps.return_value = [
            garmin_day("2026-08-30"),                   # unchanged
            garmin_day("2026-08-31", total_steps=9999),  # changed
            garmin_day("2026-09-01"),                   # new
        ]
        notion = MagicMock()
        notion.databases.query.return_value = {
            "results": [notion_page("2026-08-30"), notion_page("2026-08-31")],
            "has_more": False,
        }

        created, updated, unchanged = daily_steps.sync_daily_steps(
            garmin, notion, "db", days_back=3, today=today)

        self.assertEqual((created, updated, unchanged), (1, 1, 1))
        # One Garmin range call instead of one per day, ending yesterday.
        garmin.get_daily_steps.assert_called_once_with("2026-08-30", "2026-09-01")
        # One Notion lookup for the whole window instead of one per day.
        self.assertEqual(notion.databases.query.call_count, 1)
        notion.pages.update.assert_called_once()
        self.assertEqual(notion.pages.update.call_args.kwargs["page_id"], "page-2026-08-31")
        notion.pages.create.assert_called_once()
        created_props = notion.pages.create.call_args.kwargs["properties"]
        self.assertEqual(created_props["Date"]["date"]["start"], "2026-09-01")
        self.assertEqual(created_props["Total Distance (km)"]["number"], 6.12)


class DaysBackTests(unittest.TestCase):
    def test_defaults_to_30_days(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GARMIN_STEPS_DAYS_BACK", None)
            self.assertEqual(daily_steps.get_steps_days_back(), 30)

    def test_is_configurable(self):
        with patch.dict(os.environ, {"GARMIN_STEPS_DAYS_BACK": "500"}):
            self.assertEqual(daily_steps.get_steps_days_back(), 500)

    def test_rejects_invalid_values(self):
        with patch.dict(os.environ, {"GARMIN_STEPS_DAYS_BACK": "abc"}):
            with self.assertRaises(ValueError):
                daily_steps.get_steps_days_back()
        with patch.dict(os.environ, {"GARMIN_STEPS_DAYS_BACK": "0"}):
            with self.assertRaises(ValueError):
                daily_steps.get_steps_days_back()


if __name__ == "__main__":
    unittest.main()
