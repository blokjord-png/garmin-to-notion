import os
from datetime import date, timedelta

from dotenv import load_dotenv

from src.helpers import get_garmin_client, get_notion_client

ACTIVITY_TYPE = "Walking"
NOTION_QUERY_PAGE_SIZE = 100


def get_steps_days_back(default=30):
    """
    Number of past days to sync. Configurable via GARMIN_STEPS_DAYS_BACK so a
    one-off backfill (e.g. 500 days via workflow_dispatch) is possible without
    re-checking hundreds of days on every scheduled run.
    """
    raw_value = os.getenv("GARMIN_STEPS_DAYS_BACK", str(default))

    try:
        days_back = int(raw_value)
    except ValueError as exc:
        raise ValueError("GARMIN_STEPS_DAYS_BACK must be an integer") from exc

    if days_back < 1:
        raise ValueError("GARMIN_STEPS_DAYS_BACK must be at least 1")

    return days_back


def get_steps_start_date(days_back, today=None):
    today = today or date.today()
    return today - timedelta(days=days_back)


def get_all_daily_steps(garmin, days_back, today=None):
    """
    Get the last `days_back` days of daily step counts from Garmin Connect
    (up to and including yesterday; today's count is still in progress).

    garminconnect accepts a date range and splits it into 28-day chunks
    itself, so this is a handful of requests instead of one per day.
    """
    today = today or date.today()
    start_date = get_steps_start_date(days_back, today)
    end_date = today - timedelta(days=1)
    if end_date < start_date:
        return []
    return garmin.get_daily_steps(start_date.isoformat(), end_date.isoformat())


def get_existing_daily_steps(client, database_id, start_date):
    """
    Fetch all existing "Walking" entries since `start_date` from the Notion
    database in one paginated query, keyed by date (YYYY-MM-DD).

    Previously this was one query per day, which (together with one update per
    day) blew straight through Notion's rate limit of ~3 requests/second.
    """
    existing = {}
    start_cursor = None

    while True:
        query = client.databases.query(
            database_id=database_id,
            filter={
                "and": [
                    {"property": "Date", "date": {"on_or_after": start_date.isoformat()}},
                    {"property": "Activity Type", "title": {"equals": ACTIVITY_TYPE}},
                ]
            },
            page_size=NOTION_QUERY_PAGE_SIZE,
            **({"start_cursor": start_cursor} if start_cursor else {}),
        )

        for page in query.get('results', []):
            page_date = (
                page.get('properties', {})
                .get('Date', {})
                .get('date')
            )
            page_date = (page_date or {}).get('start')
            if not page_date:
                continue
            # Notion may return a datetime; only the calendar date matters here.
            existing.setdefault(page_date[:10], page)

        if not query.get('has_more'):
            break
        start_cursor = query.get('next_cursor')
        if not start_cursor:
            break

    return existing


def _distance_km(steps):
    total_distance = steps.get('totalDistance')
    if total_distance is None:
        total_distance = 0
    return round(total_distance / 1000, 2)


def _title_text(title_property):
    return "".join(
        part.get('plain_text') or part.get('text', {}).get('content', '')
        for part in (title_property or [])
    )


def steps_need_update(existing_steps, new_steps):
    """
    Compare existing steps data with imported data to determine if an update is needed.
    """
    existing_props = existing_steps['properties']

    return (
        existing_props['Total Steps']['number'] != new_steps.get('totalSteps') or
        existing_props['Step Goal']['number'] != new_steps.get('stepGoal') or
        existing_props['Total Distance (km)']['number'] != _distance_km(new_steps) or
        _title_text(existing_props['Activity Type']['title']) != ACTIVITY_TYPE
    )


def update_daily_steps(client, existing_steps, new_steps):
    """
    Update an existing daily steps entry in the Notion database with new data.
    """
    properties = {
        "Activity Type": {"title": [{"text": {"content": ACTIVITY_TYPE}}]},
        "Total Steps": {"number": new_steps.get('totalSteps')},
        "Step Goal": {"number": new_steps.get('stepGoal')},
        "Total Distance (km)": {"number": _distance_km(new_steps)}
    }

    update = {
        "page_id": existing_steps['id'],
        "properties": properties,
    }

    client.pages.update(**update)


def create_daily_steps(client, database_id, steps):
    """
    Create a new daily steps entry in the Notion database.
    """
    properties = {
        "Activity Type": {"title": [{"text": {"content": ACTIVITY_TYPE}}]},
        "Date": {"date": {"start": steps.get('calendarDate')}},
        "Total Steps": {"number": steps.get('totalSteps')},
        "Step Goal": {"number": steps.get('stepGoal')},
        "Total Distance (km)": {"number": _distance_km(steps)}
    }

    page = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }

    client.pages.create(**page)


def sync_daily_steps(garmin_client, notion_client, database_id, days_back, today=None):
    today = today or date.today()
    start_date = get_steps_start_date(days_back, today)

    print(f"Syncing Garmin daily steps for the last {days_back} days "
          f"({start_date.isoformat()} → {(today - timedelta(days=1)).isoformat()})")

    daily_steps = get_all_daily_steps(garmin_client, days_back, today)
    existing_by_date = get_existing_daily_steps(notion_client, database_id, start_date)

    created = updated = unchanged = skipped = 0
    for steps in daily_steps:
        steps_date = steps.get('calendarDate')
        if not steps_date:
            skipped += 1
            continue

        existing_steps = existing_by_date.get(steps_date)
        if existing_steps:
            if steps_need_update(existing_steps, steps):
                update_daily_steps(notion_client, existing_steps, steps)
                updated += 1
                print(f"Updated daily steps for {steps_date}: {steps.get('totalSteps')} steps")
            else:
                unchanged += 1
        else:
            create_daily_steps(notion_client, database_id, steps)
            created += 1
            print(f"Created daily steps for {steps_date}: {steps.get('totalSteps')} steps")

    print(f"Daily steps sync done: {created} created, {updated} updated, "
          f"{unchanged} unchanged, {skipped} skipped.")

    return created, updated, unchanged


def main():
    load_dotenv()

    # Initialize Garmin and Notion clients using environment variables
    garmin_client, _ = get_garmin_client()
    notion_client, notion_dbs = get_notion_client()

    sync_daily_steps(
        garmin_client,
        notion_client,
        notion_dbs.daily_steps,
        get_steps_days_back(),
    )


if __name__ == '__main__':
    main()
