import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

from automation import schedule, tools

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:lecture-1
DTSTART;TZID=Asia/Kolkata:20261105T140000
DTEND;TZID=Asia/Kolkata:20261105T150000
RRULE:FREQ=WEEKLY;BYDAY=TH
SUMMARY:AI Lecture
LOCATION:AB1 204
END:VEVENT
BEGIN:VEVENT
UID:fest
DTSTART;VALUE=DATE:20261119
DTEND;VALUE=DATE:20261120
SUMMARY:Tech Fest
END:VEVENT
BEGIN:VEVENT
UID:dinner
DTSTART;TZID=Asia/Kolkata:20261119T200000
DTEND;TZID=Asia/Kolkata:20261119T210000
SUMMARY:Family dinner
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART;TZID=Asia/Kolkata:20261119T160000
DTEND;TZID=Asia/Kolkata:20261119T170000
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


class ScheduleTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 19, 12, 0)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.folder.name, "TZ": "Asia/Kolkata",
                                           "JARVIS_CALENDAR_ICS_URL": ""})
        self.env.start()
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()

    def tearDown(self):
        self.env.stop()
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()
        self.folder.cleanup()

    def test_day_windows(self):
        self.assertEqual(schedule.day_window("today", "afternoon", self.NOW),
                         (datetime.datetime(2026, 11, 19, 12), datetime.datetime(2026, 11, 19, 17)))
        self.assertEqual(schedule.day_window("tomorrow", "all", self.NOW)[0], datetime.datetime(2026, 11, 20))
        self.assertEqual(schedule.day_window("2026-11-25", "evening", self.NOW)[1], datetime.datetime(2026, 11, 25, 21))

    def test_reads_recurring_and_all_day_events_and_skips_cancelled(self):
        start, end = datetime.datetime(2026, 11, 19), datetime.datetime(2026, 11, 20)
        events = schedule.fetch_calendar_events(start, end, ics_text=ICS)
        titles = [(e["title"], e["start"].strftime("%H:%M"), e["all_day"]) for e in events]
        self.assertIn(("AI Lecture", "14:00", False), titles)  # weekly repeat lands on this Thursday
        self.assertIn(("Tech Fest", "00:00", True), titles)
        self.assertNotIn("Cancelled thing", [t[0] for t in titles])

    def test_afternoon_schedule_merges_calendar_and_jarvis_events(self):
        schedule.save_local_event("Meeting with Rahul", datetime.datetime(2026, 11, 19, 15),
                                  datetime.datetime(2026, 11, 19, 16), 10)
        real_fetch = schedule.fetch_calendar_events
        with patch.object(schedule, "fetch_calendar_events", lambda s, e: real_fetch(s, e, ics_text=ICS)), \
                patch.object(schedule, "_now", return_value=self.NOW):
            text = tools.get_schedule("today", "afternoon")
        self.assertEqual(
            text,
            "You have 3 things this afternoon: Tech Fest (all day); AI Lecture at 2:00 PM in AB1 204; "
            "Meeting with Rahul at 3:00 PM.",
        )

    def test_without_calendar_link_it_says_what_it_can_see(self):
        with patch.object(schedule, "_now", return_value=self.NOW):
            text = tools.get_schedule("today", "evening")
        self.assertIn("You have nothing scheduled this evening.", text)
        self.assertIn("JARVIS_CALENDAR_ICS_URL", text)

    def test_scheduling_saves_event_and_restart_restores_reminder(self):
        start = (datetime.datetime.now() + datetime.timedelta(hours=2)).replace(second=0, microsecond=0)
        with patch.object(tools, "_open_url"), patch.object(tools, "set_reminder"):
            tools.schedule_event("Viva", start.strftime("%Y-%m-%dT%H:%M"), 30)
        self.assertEqual([e["title"] for e in schedule.load_local_events()], ["Viva"])
        with patch.object(tools, "set_reminder") as reminder:
            self.assertEqual(tools.restore_reminders(), 1)
        self.assertAlmostEqual(reminder.call_args.args[0], 110, delta=1.5)
        self.assertEqual(reminder.call_args.args[1], "Viva starts in 10 minutes.")


if __name__ == "__main__":
    unittest.main()


class CalendarLinkTests(unittest.TestCase):
    def test_browser_address_is_rejected_with_clear_help(self):
        import datetime, os
        from unittest.mock import patch
        from automation import schedule
        with patch.dict(os.environ, {"JARVIS_CALENDAR_ICS_URL": "https://calendar.google.com/calendar/u/0/r"}):
            with self.assertRaises(schedule.ScheduleError) as error:
                schedule.fetch_calendar_events(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2))
        self.assertIn("Secret address in iCal format", str(error.exception))

    def test_sign_in_page_is_not_dumped(self):
        import datetime
        from automation import schedule
        page = b"<!doctype html><html><title>Google Calendar - Sign in</title>" + b"x" * 50000
        with self.assertRaises(schedule.ScheduleError) as error:
            schedule.fetch_calendar_events(datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2), page)
        self.assertLess(len(str(error.exception)), 400)
