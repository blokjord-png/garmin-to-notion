import os
from datetime import datetime, date, timedelta
from time import sleep

import pytz
from dotenv import load_dotenv, dotenv_values

from src.helpers import get_garmin_client, get_notion_client

# Constants
local_tz = pytz.timezone("Europe/Brussels")

# Load environment variables
load_dotenv()
CONFIG = dotenv_values()


def get_sleep_data_for_date(garmin, target_date):
    return garmin.get_sleep_data(target_date.isoformat())


def get_sleep_dates(days_back=500):
    startdate = date.today() - timedelta(days=days_back)
    return [startdate + timedelta(days=x)
            for x in range((date.today() - startdate).days + 1)]


def get_sleep_days_back(default=30):
    raw_value = os.getenv("GARMIN_SLEEP_DAYS_BACK", str(default))

    try:
        days_back = int(raw_value)
    except ValueError as exc:
        raise ValueError("GARMIN_SLEEP_DAYS_BACK must be an integer") from exc

    if days_back < 0:
        raise ValueError("GARMIN_SLEEP_DAYS_BACK must be zero or greater")

    return days_back


def format_duration(seconds):
    minutes = (seconds or 0) // 60
    return f"{minutes // 60}h {minutes % 60}m"


def format_time(timestamp):
    return (
        datetime.utcfromtimestamp(timestamp / 1000).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if timestamp else None
    )


def format_time_readable(timestamp):
    return (
        datetime.fromtimestamp(timestamp / 1000, local_tz).strftime("%H:%M")
        if timestamp else "Unknown"
    )


def format_date_for_name(sleep_date):
    return datetime.strptime(sleep_date, "%Y-%m-%d").strftime("%d.%m.%Y") if sleep_date else "Unknown"


def get_sleep_score(daily_sleep):
    sleep_scores = daily_sleep.get('sleepScores') or {}
    overall = sleep_scores.get('overall') or {}
    value = overall.get('value')

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    if not 0 <= value <= 100:
        print(f"Ignoring invalid Garmin sleep score: {value}")
        return None

    return value


def sleep_data_exists(client, database_id, sleep_date):
    query = client.databases.query(
        database_id=database_id,
        filter={"property": "Long Date", "date": {"equals": sleep_date}}
    )
    results = query.get('results', [])
    return results[0] if results else None  # Ensure it returns None instead of causing IndexError


def update_sleep_score(client, existing_page, daily_sleep, sleep_date):
    sleep_score = get_sleep_score(daily_sleep)

    if sleep_score is None:
        print(f"No Garmin sleep score available for: {sleep_date}")
        return False

    existing_score = (
        existing_page.get('properties', {})
        .get('Sleep Score', {})
        .get('number')
    )

    if existing_score == sleep_score:
        print(f"Sleep score already up to date for: {sleep_date}")
        return False

    page_id = existing_page.get('id')
    if not page_id:
        print(f"Cannot update sleep score for {sleep_date}: Notion page ID is missing")
        return False

    client.pages.update(
        page_id=page_id,
        properties={"Sleep Score": {"number": sleep_score}},
    )
    print(f"Updated sleep score for: {sleep_date} ({sleep_score}/100)")
    return True


def create_sleep_data(client, database_id, sleep_data, skip_zero_sleep=True):
    daily_sleep = sleep_data.get('dailySleepDTO', {})
    if not daily_sleep:
        return

    sleep_date = daily_sleep.get('calendarDate', "Unknown Date")
    total_sleep = sum(
        (daily_sleep.get(k, 0) or 0) for k in ['deepSleepSeconds', 'lightSleepSeconds', 'remSleepSeconds']
    )

    if skip_zero_sleep and total_sleep == 0:
        print(f"Skipping sleep data for {sleep_date} as total sleep is 0")
        return

    properties = {
        "Date": {"title": [{"text": {"content": format_date_for_name(sleep_date)}}]},
        "Times": {"rich_text": [{"text": {
            "content": f"{format_time_readable(daily_sleep.get('sleepStartTimestampGMT'))} → {format_time_readable(daily_sleep.get('sleepEndTimestampGMT'))}"}}]},
        "Long Date": {"date": {"start": sleep_date}},
        "Full Date/Time": {"date": {"start": format_time(daily_sleep.get('sleepStartTimestampGMT')),
                                    "end": format_time(daily_sleep.get('sleepEndTimestampGMT'))}},
        "Total Sleep (h)": {"number": round(total_sleep / 3600, 1)},
        "Light Sleep (h)": {"number": round(daily_sleep.get('lightSleepSeconds', 0) / 3600, 1)},
        "Deep Sleep (h)": {"number": round(daily_sleep.get('deepSleepSeconds', 0) / 3600, 1)},
        "REM Sleep (h)": {"number": round(daily_sleep.get('remSleepSeconds', 0) / 3600, 1)},
        "Awake Time (h)": {"number": round(daily_sleep.get('awakeSleepSeconds', 0) / 3600, 1)},
        "Total Sleep": {"rich_text": [{"text": {"content": format_duration(total_sleep)}}]},
        "Light Sleep": {"rich_text": [{"text": {"content": format_duration(daily_sleep.get('lightSleepSeconds', 0))}}]},
        "Deep Sleep": {"rich_text": [{"text": {"content": format_duration(daily_sleep.get('deepSleepSeconds', 0))}}]},
        "REM Sleep": {"rich_text": [{"text": {"content": format_duration(daily_sleep.get('remSleepSeconds', 0))}}]},
        "Awake Time": {"rich_text": [{"text": {"content": format_duration(daily_sleep.get('awakeSleepSeconds', 0))}}]},
        "Resting HR": {"number": sleep_data.get('restingHeartRate', 0)}
    }

    sleep_score = get_sleep_score(daily_sleep)
    if sleep_score is not None:
        properties["Sleep Score"] = {"number": sleep_score}

    client.pages.create(parent={"database_id": database_id}, properties=properties, icon={"emoji": "😴"})
    print(f"Created sleep entry for: {sleep_date}")


def main():
    load_dotenv()

    garmin_client, _ = get_garmin_client()
    notion_client, notion_dbs = get_notion_client()

    database_id = notion_dbs.sleep

    days_back = get_sleep_days_back()
    print(f"Checking Garmin sleep data for the last {days_back} days")
    sleep_dates = get_sleep_dates(days_back=days_back)

    for d in sleep_dates:
        print(f"Checking sleep data for {d.isoformat()}")

        data = get_sleep_data_for_date(garmin_client, d)

        if data:
            sleep_date = data.get('dailySleepDTO', {}).get('calendarDate')

            if sleep_date:
                existing_page = sleep_data_exists(notion_client, database_id, sleep_date)

                if existing_page:
                    update_sleep_score(
                        notion_client,
                        existing_page,
                        data.get('dailySleepDTO', {}),
                        sleep_date,
                    )
                else:
                    create_sleep_data(notion_client, database_id, data, skip_zero_sleep=True)
            else:
                print(f"No valid sleep date for {d.isoformat()}")

        sleep(0.5)

if __name__ == '__main__':
    main()
