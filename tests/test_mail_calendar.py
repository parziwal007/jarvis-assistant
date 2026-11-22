import datetime
import json
import os
import tempfile
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import main
from automation import app, mail, presence, tools
from brain import agent, llm


def make_email(sender, subject, text=None, html_text=None):
    if html_text:
        message = MIMEMultipart("alternative")
        message.attach(MIMEText(html_text, "html"))
    else:
        message = MIMEText(text or "")
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = "Thu, 19 Nov 2026 10:15:00 +0530"
    return message.as_bytes()


class FakeImap:
    """Minimal stand-in for imaplib.IMAP4_SSL."""

    mailbox = {}
    selected_readonly = None

    def __init__(self, host, timeout=None):
        self.host = host

    def login(self, user, password):
        self.user, self.password = user, password

    def select(self, folder, readonly=False):
        FakeImap.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            criteria = " ".join(a for a in args if a)
            uids = sorted(FakeImap.mailbox)
            if "UID" in criteria:
                low = int(criteria.split("UID ")[1].split(":")[0])
                uids = [u for u in uids if u >= low] or uids[-1:]  # real servers return the last message for N:*
            if "SINCE" in criteria:
                pass  # all test mail is recent
            if "UNSEEN" in criteria:
                uids = [u for u in uids if not FakeImap.mailbox[u][1]]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "fetch":
            uid = int(args[0])
            return "OK", [(f"{uid} (UID {uid} BODY[]<0>".encode(), FakeImap.mailbox[uid][0]), b")"]
        raise AssertionError(command)

    def logout(self):
        pass


MAIL_ENV = {"JARVIS_EMAIL_ADDRESS": "me@gmail.com", "JARVIS_EMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}


class MailTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {**MAIL_ENV, "LOCALAPPDATA": self.folder.name})
        self.env.start()
        self.imap = patch.object(mail.imaplib, "IMAP4_SSL", FakeImap)
        self.imap.start()
        FakeImap.mailbox = {
            41: (make_email("Amazon <deals@amazon.in>", "50% off headphones", "Big sale today"), False),
            42: (make_email("Prof. Rao <rao@vitap.ac.in>", "Lab exam moved",
                            html_text="<p>The lab exam is moved to <b>Friday 10 AM</b>.</p><style>p{}</style>"), False),
            40: (make_email("Old <old@x.com>", "Already read", "hi"), True),
        }

    def tearDown(self):
        self.imap.stop()
        self.env.stop()
        self.folder.cleanup()

    def test_fetch_is_read_only_newest_first_and_parses_html(self):
        messages = mail.fetch_messages(unread_only=True, limit=5)
        self.assertTrue(FakeImap.selected_readonly)
        self.assertEqual([m["uid"] for m in messages], [42, 41])
        self.assertEqual(messages[0]["from"], "Prof. Rao")
        self.assertEqual(messages[0]["body"], "The lab exam is moved to Friday 10 AM .")

    def test_mail_from_yesterday_includes_already_read_mail(self):
        with patch.object(llm, "generate_text", return_value="summary") as generate:
            tools.check_email(days=1)
        self.assertIn("Already read", generate.call_args.args[0])

    def test_not_configured_gives_setup_instructions(self):
        with patch.dict(os.environ, {"JARVIS_EMAIL_APP_PASSWORD": ""}):
            with self.assertRaisesRegex(tools.ToolError, "JARVIS_EMAIL_APP_PASSWORD"):
                tools.check_email()

    def test_check_email_summarises_with_the_model(self):
        with patch.object(llm, "generate_text", return_value="Prof. Rao moved the lab exam to Friday.") as generate:
            self.assertEqual(tools.check_email(), "Prof. Rao moved the lab exam to Friday.")
        self.assertIn("Lab exam moved", generate.call_args.args[0])

    def test_watcher_first_run_sets_baseline_then_announces_only_important_new_mail(self):
        announced = []
        watcher = mail.MailWatcher(announced.append)
        presence.set_away(False)
        self.addCleanup(presence.set_away, None)
        with patch.object(mail, "show_windows_notification") as toast, \
                patch.object(mail, "draft_reply", return_value="Monday 11 works for me. Sarthak") as draft:
            self.assertEqual(watcher.check_once(), [])  # baseline: no flood of old mail
            self.assertEqual(json.loads(mail._state_path().read_text())["last_uid"], 42)
            self.assertEqual(watcher.check_once(), [])  # nothing new yet

            FakeImap.mailbox[43] = (make_email("Insta <no-reply@instagram.com>", "New likes", "x"), False)
            FakeImap.mailbox[44] = (make_email("HR <hr@company.com>", "Interview invite", "Monday 11 AM"), False)
            verdict = json.dumps([
                {"n": 1, "important": False, "summary": "Instagram likes"},
                {"n": 2, "important": True, "summary": "HR: interview invite for Monday 11 AM"},
            ])
            with patch.object(llm, "complete", return_value=(verdict, True)):
                alerts = watcher.check_once()
        self.assertEqual(alerts, ["HR: interview invite for Monday 11 AM"])
        self.assertTrue(announced[0].startswith("Important email: HR: interview invite for Monday 11 AM."))
        self.assertIn("I've drafted a reply: Monday 11 works for me. Sarthak", announced[0])
        self.assertEqual(draft.call_args.args[0]["subject"], "Interview invite")
        self.assertEqual(tools.get_pending()["kind"], "email_reply")
        self.assertEqual(tools.get_pending()["to"], "hr@company.com")
        tools.clear_pending()
        toast.assert_called_once()
        self.assertEqual(json.loads(mail._state_path().read_text())["last_uid"], 44)

    def test_watcher_falls_back_to_subjects_when_model_is_down(self):
        announced = []
        watcher = mail.MailWatcher(announced.append)
        mail._save_state({"last_uid": 42})
        FakeImap.mailbox[43] = (make_email("Mom <mom@x.com>", "Call me", "when free"), False)
        with patch.object(mail, "show_windows_notification"), patch.object(llm, "complete", return_value=("down", False)):
            self.assertEqual(watcher.check_once(), ["Mom: Call me"])


class FakeSmtp:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.password = password

    def send_message(self, message):
        FakeSmtp.sent.append(message)


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {**MAIL_ENV, "LOCALAPPDATA": self.folder.name, "JARVIS_USER_NAME": "Sarthak"})
        self.env.start()
        patch.object(mail.imaplib, "IMAP4_SSL", FakeImap).start()
        patch.object(mail.smtplib, "SMTP_SSL", FakeSmtp).start()
        self.contacts = Path(self.folder.name) / "contacts.json"
        self.contacts.write_text(json.dumps({"adi": {"phone": "919876500001", "email": "rahul@example.com"}}))
        patch.object(tools, "CONTACTS_FILE", self.contacts).start()
        patch.object(mail, "show_windows_notification").start()
        self.addCleanup(patch.stopall)
        FakeSmtp.sent = []
        tools.clear_pending()
        adi = make_email("Rahul Verma <rahul@example.com>", "Project call?", "What time works for a Zoom call?")
        adi = adi.replace(b"Subject:", b"Message-ID: <abc@mail.gmail.com>\nSubject:", 1)
        FakeImap.mailbox = {
            50: (make_email("Google <no-reply@accounts.google.com>", "Security alert", "Password changed"), False),
            51: (adi, True),
        }

    def tearDown(self):
        presence.set_away(None)
        tools.clear_pending()
        self.env.stop()
        self.folder.cleanup()

    def test_read_mail_from_one_person_uses_contacts_and_reads_it_in_full(self):
        result = tools.check_email(from_name="adi", read_full=True)
        self.assertIn("Email from Rahul Verma", result)
        self.assertIn("What time works for a Zoom call?", result)
        self.assertNotIn("Security alert", result)

    def test_reply_to_email_drafts_and_send_it_sends_threaded_reply(self):
        with patch.object(llm, "generate_text", return_value="Hi Rahul, 6 PM works. Sarthak") as generate:
            spoken = tools.reply_to_email("adi", "say 6 pm works")
        self.assertIn("say 6 pm works", generate.call_args.args[0])
        self.assertIn("6 PM works", spoken)
        self.assertEqual(FakeSmtp.sent, [])  # nothing sent before "send it"
        self.assertEqual(app.trigger_action("Jarvis, send it"), "Sent your reply to Rahul Verma.")
        sent = FakeSmtp.sent[0]
        self.assertEqual(sent["To"], "rahul@example.com")
        self.assertEqual(sent["Subject"], "Re: Project call?")
        self.assertEqual(sent["In-Reply-To"], "<abc@mail.gmail.com>")
        self.assertIsNone(sent["Auto-Submitted"])
        self.assertEqual(app.trigger_action("send it"), "There's nothing waiting to be sent.")

    def test_cancel_discards_the_pending_reply(self):
        tools.set_pending("email_reply", "your reply to Adi", to="a@b.com", subject="Re: x", body="hi")
        self.assertEqual(app.trigger_action("don't send it"), "Okay, I won't send it.")
        self.assertEqual(tools.get_pending(), {})

    def test_send_it_sends_whatsapp_when_that_was_prepared_last(self):
        tools.set_pending("email_draft", "the email to Adi")
        tools.set_pending("whatsapp", "the WhatsApp message to Adi")
        with patch.object(tools, "whatsapp_send", return_value="Sent.") as whatsapp, \
                patch.object(tools, "email_send") as email_send:
            self.assertEqual(main.handle_command("send it", source="voice")[0], "Sent.")
        whatsapp.assert_called_once()
        email_send.assert_not_called()

    def test_away_auto_reply_goes_to_people_once_a_day_never_to_automated_mail(self):
        presence.set_away(True)
        mail._save_state({"last_uid": 51})
        FakeImap.mailbox[52] = (make_email("Prof. Rao <rao@vitap.ac.in>", "Viva tomorrow", "Please confirm."), False)
        FakeImap.mailbox[53] = (make_email("Bank <alerts@hdfcbank.net>", "Low balance", "Rs 20"), False)
        verdict = json.dumps([{"n": 1, "important": True, "summary": "Prof. Rao: viva tomorrow"},
                              {"n": 2, "important": True, "summary": "HDFC: low balance"}])
        announced = []
        watcher = mail.MailWatcher(announced.append)
        with patch.object(llm, "complete", return_value=(verdict, True)):
            watcher.check_once()
        self.assertEqual([m["To"] for m in FakeSmtp.sent], ["rao@vitap.ac.in"])
        reply = FakeSmtp.sent[0]
        self.assertEqual(reply["Subject"], "Re: Viva tomorrow")
        self.assertEqual(reply["Auto-Submitted"], "auto-replied")
        self.assertTrue(reply.get_content().startswith("Hi Prof. Rao,"))
        self.assertIn("Sarthak isn't available at the moment", reply.get_content())
        self.assertIn("I told Prof. Rao that you'll get back to them", announced[0])

        FakeImap.mailbox[54] = (make_email("Prof. Rao <rao@vitap.ac.in>", "Re: Viva tomorrow", "Reminder"), False)
        with patch.object(llm, "complete", return_value=(json.dumps([{"n": 1, "important": True, "summary": "x"}]), True)):
            watcher.check_once()
        self.assertEqual(len(FakeSmtp.sent), 1)  # not twice in one day

    def test_quoted_thread_is_not_read_out(self):
        raw = make_email("Rahul Verma <rahul@example.com>", "Re: Your message",
                         "I am not available today.\n\nOn Thu, 19 Nov 2026 at 13:25, <me@example.com> wrote:\n"
                         "> Let's schedule a brief chat this afternoon.\n> Sarthak\n")
        self.assertEqual(mail.parse_message(1, raw)["body"], "I am not available today.")
        html_raw = make_email("A <a@b.com>", "Re: x", html_text="<div>Sounds good!</div><blockquote>old stuff</blockquote>")
        self.assertEqual(mail.parse_message(2, html_raw)["body"], "Sounds good!")

    def test_send_it_refuses_when_a_different_person_is_named(self):
        self.contacts.write_text(json.dumps({
            "adi": {"email": "rahul@example.com"}, "priya": {"email": "priya@example.com"}}))
        tools.set_pending("email_reply", "your reply to Rahul Verma", to="rahul@example.com",
                          subject="Re: x", body="hi")
        with self.assertRaisesRegex(tools.ToolError, "not to priya"):
            tools.send_pending(to="priya")
        with self.assertRaisesRegex(tools.ToolError, "not to children"):
            tools.send_pending(to="children")
        self.assertEqual(FakeSmtp.sent, [])
        self.assertEqual(tools.send_pending(to="Rahul"), "Sent your reply to Rahul Verma.")
        tools.set_pending("email_reply", "your reply to Rahul Verma", to="rahul@example.com",
                          subject="Re: x", body="hi")
        self.assertEqual(tools.send_pending(to="adi"), "Sent your reply to Rahul Verma.")  # adi = same address

    def test_greeting_names(self):
        self.assertEqual(mail.first_name("Rahul Verma"), "Rahul")
        self.assertEqual(mail.first_name("Dr. Meena Iyer"), "Dr. Iyer")
        self.assertEqual(mail.first_name("rao@vitap.ac.in"), "there")

    def test_availability_by_voice(self):
        self.assertIn("you're away", tools.set_availability("away"))
        self.assertTrue(presence.is_away())
        tools.set_availability("back")
        self.assertFalse(presence.is_away())


class ConversationTests(unittest.TestCase):
    def setUp(self):
        llm.CONVERSATION_HISTORY.clear()

    def test_background_chatter_gets_silence_when_spoken(self):
        silent = json.dumps({"actions": [], "reply": ""})
        with patch.object(llm, "complete", return_value=(silent, True)) as complete:
            self.assertEqual(agent.handle("uh-huh what's all that", source="voice"), "")
            self.assertIn("picks up other people talking", complete.call_args.args[1])
            self.assertEqual(llm.CONVERSATION_HISTORY, [])  # chatter isn't remembered
            self.assertIn("didn't catch that", agent.handle("uh-huh what's all that", source="typed"))
            self.assertNotIn("picks up other people talking", complete.call_args.args[1])

    def test_main_stays_silent_for_chatter(self):
        main._PENDING_ACTION = None
        with patch.object(agent, "handle", return_value=""):
            self.assertEqual(main.handle_command("do you wanna miss", source="voice"), ("", False))


class CalendarTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 19, 12, 0)

    def test_parse_times(self):
        self.assertEqual(tools.parse_event_time("3", self.NOW), datetime.datetime(2026, 11, 19, 15, 0))
        self.assertEqual(tools.parse_event_time("3:30 pm", self.NOW), datetime.datetime(2026, 11, 19, 15, 30))
        self.assertEqual(tools.parse_event_time("9 am", self.NOW), datetime.datetime(2026, 11, 20, 9, 0))
        self.assertEqual(tools.parse_event_time("2026-11-21T10:00", self.NOW), datetime.datetime(2026, 11, 21, 10, 0))
        with self.assertRaises(tools.ToolError):
            tools.parse_event_time("sometime", self.NOW)

    def test_schedule_event_opens_prefilled_calendar_and_sets_reminder(self):
        start = (datetime.datetime.now() + datetime.timedelta(hours=3)).replace(second=0, microsecond=0)
        with patch.object(tools, "_open_url") as open_url, patch.object(tools, "set_reminder") as reminder:
            message = tools.schedule_event("Project meeting", start.strftime("%Y-%m-%dT%H:%M"), 30, "With guide")
        query = parse_qs(urlparse(open_url.call_args.args[0]).query)
        self.assertEqual(query["action"], ["TEMPLATE"])
        self.assertEqual(query["text"], ["Project meeting"])
        utc = datetime.timezone.utc
        expected_start = start.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        expected_end = (start + datetime.timedelta(minutes=30)).astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        self.assertEqual(query["dates"], [f"{expected_start}/{expected_end}"])
        self.assertAlmostEqual(reminder.call_args.args[0], 170, delta=1.5)
        self.assertIn("Click Save", message)

    def test_email_send_refuses_without_a_mail_window(self):
        with patch.object(tools, "IS_WINDOWS", True), patch.object(tools, "_leave_jarvis_window"), \
                patch.object(tools, "foreground_window_title", return_value="Spotify"), \
                patch.object(tools.time, "sleep"), patch.object(tools, "_press_combo") as press:
            with self.assertRaisesRegex(tools.ToolError, "didn't send"):
                tools.email_send()
        press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
