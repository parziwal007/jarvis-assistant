"""Your schedule: Google Calendar (read through its private iCal address) plus the events
Jarvis scheduled itself.

Setup (.env), optional but recommended:
  JARVIS_CALENDAR_ICS_URL=https://calendar.google.com/calendar/ical/.../basic.ics
Get it from Google Calendar > Settings > (your calendar) > Integrate calendar >
"Secret address in iCal format". Keep it private: anyone with it can read your calendar.

Events Jarvis schedules are also saved locally, so they show up at once (Google's iCal
feed can take a few minutes to update) and their reminders survive a restart.
"""
import datetime
import json
import os
import urllib.request
from pathlib import Path

PARTS_OF_DAY = {
    "morning": (5, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 24),
    "all": (0, 24),
    "day": (0, 24),
}


def _now():
    return datetime.datetime.now()


class ScheduleError(Exception):
    """A user-facing calendar problem."""


def _local_path():
    base = os.getenv("LOCALAPPDATA")
    folder = Path(base) / "JarvisAssistant" if base else Path.home() / ".cache" / "jarvis_assistant"
    return folder / "events.json"


def load_local_events():
    try:
        data = json.loads(_local_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    events = []
    for item in data if isinstance(data, list) else []:
        try:
            events.append({
                "title": str(item["title"]),
                "start": datetime.datetime.fromisoformat(item["start"]),
                "end": datetime.datetime.fromisoformat(item["end"]),
                "remind": int(item.get("remind", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return events


def save_local_event(title, start, end, remind_minutes):
    """Remembers an event Jarvis scheduled (keeps the last 60 days)."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=60)
    events = [event for event in load_local_events() if event["end"] > cutoff]
    events.append({"title": title, "start": start, "end": end, "remind": remind_minutes})
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"title": e["title"], "start": e["start"].isoformat(timespec="minutes"),
         "end": e["end"].isoformat(timespec="minutes"), "remind": e["remind"]}
        for e in events
    ], indent=1), encoding="utf-8")


def _to_local_naive(value, is_end=False):
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value
    # all-day events come as dates
    return datetime.datetime.combine(value, datetime.time.min)


WRONG_LINK = ("The calendar link in .env isn't your calendar's secret iCal address, so Google asked for a sign-in "
              "instead. In Google Calendar settings, open your calendar, go to Integrate calendar, and copy the "
              "Secret address in iCal format. It ends in basic.ics.")


def fetch_calendar_events(start, end, ics_text=None):
    """Events from the Google Calendar iCal feed between start and end (local naive datetimes)."""
    url = os.getenv("JARVIS_CALENDAR_ICS_URL", "").strip()
    if ics_text is None:
        if not url:
            return None  # not configured
        if "calendar.google.com" in url and "/ical/" not in url:
            raise ScheduleError(WRONG_LINK)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "JarvisAssistant"})
            with urllib.request.urlopen(request, timeout=15) as response:
                ics_text = response.read()
        except OSError as exc:
            raise ScheduleError(f"I couldn't reach your Google Calendar: {str(exc)[:150]}") from exc
    head = (ics_text[:2000].decode("utf-8", "replace") if isinstance(ics_text, bytes) else str(ics_text)[:2000])
    if "BEGIN:VCALENDAR" not in head:
        # e.g. a Google sign-in page: the link isn't the secret iCal address
        raise ScheduleError(WRONG_LINK)
    try:
        import icalendar
        import recurring_ical_events
    except ImportError as exc:
        raise ScheduleError("Calendar reading needs two packages. Run: python -m pip install icalendar "
                            "recurring-ical-events") from exc
    try:
        calendar = icalendar.Calendar.from_ical(ics_text)
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except Exception as exc:
        raise ScheduleError(f"I couldn't read your calendar feed: {str(exc)[:150]}") from exc
    events = []
    for item in occurrences:
        if str(item.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begin = item.get("DTSTART").dt
        finish = item.get("DTEND").dt if item.get("DTEND") else begin
        all_day = not isinstance(begin, datetime.datetime)
        events.append({
            "title": str(item.get("SUMMARY", "(no title)")),
            "start": _to_local_naive(begin),
            "end": _to_local_naive(finish),
            "all_day": all_day,
            "location": str(item.get("LOCATION", "") or ""),
        })
    return events


def day_window(day="today", part="all", now=None):
    now = now or _now()
    text = str(day or "today").strip().lower()
    if text in {"today", ""}:
        date = now.date()
    elif text == "tomorrow":
        date = now.date() + datetime.timedelta(days=1)
    elif text == "yesterday":
        date = now.date() - datetime.timedelta(days=1)
    else:
        try:
            date = datetime.date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ScheduleError(f"I couldn't understand the day {day!r}.") from exc
    first, last = PARTS_OF_DAY.get(str(part or "all").strip().lower(), (0, 24))
    start = datetime.datetime.combine(date, datetime.time(first))
    end = datetime.datetime.combine(date, datetime.time.min) + datetime.timedelta(hours=last)
    return start, end


def events_between(start, end):
    """Calendar + Jarvis-scheduled events overlapping [start, end), sorted, without duplicates.

    Returns (events, calendar_connected)."""
    calendar_events = fetch_calendar_events(start, end)
    connected = calendar_events is not None
    merged = list(calendar_events or [])
    seen = {(e["title"].strip().casefold(), e["start"]) for e in merged}
    for event in load_local_events():
        key = (event["title"].strip().casefold(), event["start"])
        if event["start"] < end and event["end"] > start and key not in seen:
            merged.append({**event, "all_day": False, "location": ""})
            seen.add(key)
    merged.sort(key=lambda e: (not e["all_day"], e["start"]))
    return merged, connected


def describe(events, day_label, part_label, connected):
    when = f"{day_label}{'' if part_label in {'all', 'day', ''} else ' ' + part_label}"
    if not events:
        text = f"You have nothing scheduled {when}."
    else:
        parts = []
        for event in events:
            if event["all_day"]:
                parts.append(f"{event['title']} (all day)")
            else:
                time_text = event["start"].strftime("%I:%M %p").lstrip("0")
                parts.append(f"{event['title']} at {time_text}" + (f" in {event['location']}" if event["location"] else ""))
        text = f"You have {len(events)} thing{'s' if len(events) != 1 else ''} {when}: " + "; ".join(parts) + "."
    if not connected:
        text += (" I can only see events I scheduled myself. To include your whole Google Calendar, add "
                 "JARVIS_CALENDAR_ICS_URL to .env.")
    return text
