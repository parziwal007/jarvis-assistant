import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import main
from automation import app, tools
from brain import agent, llm


def plan(actions=(), reply=""):
    return json.dumps({"actions": list(actions), "reply": reply})


class ParsePlanTests(unittest.TestCase):
    def test_parses_fenced_json(self):
        text = '```json\n{"actions": [{"tool": "open_app", "args": {"name": "spotify"}}], "reply": "Opening."}\n```'
        self.assertEqual(
            agent.parse_plan(text),
            {"actions": [{"tool": "open_app", "args": {"name": "spotify"}}], "reply": "Opening."},
        )

    def test_accepts_other_argument_spellings(self):
        for raw in [
            '{"actions": [{"tool": "open_app", "arguments": {"name": "youtube"}}], "reply": ""}',
            '{"actions": [{"tool": "open_app", "parameters": {"name": "youtube"}}], "reply": ""}',
            '{"actions": [{"tool": "open_app", "arguments": "{\\"name\\": \\"youtube\\"}"}], "reply": ""}',
            '{"actions": [{"name": "open_app", "args": {"name": "youtube"}}], "reply": ""}',
            '{"actions": [{"tool": "open_app", "name": "youtube"}], "reply": ""}',
            '{"actions": [{"type": "function", "function": {"name": "open_app", "arguments": "{\\"name\\": \\"youtube\\"}"}}], "reply": ""}',
        ]:
            self.assertEqual(agent.parse_plan(raw)["actions"], [{"tool": "open_app", "args": {"name": "youtube"}}], raw)

    def test_rejects_non_json(self):
        self.assertIsNone(agent.parse_plan("Sure! I'll open Spotify for you."))

    def test_drops_malformed_actions_and_caps_count(self):
        actions = [{"tool": "wait", "args": {"seconds": 1}}] * 10 + [{"args": {}}, "bad"]
        parsed = agent.parse_plan(plan(actions, "ok"))
        self.assertEqual(len(parsed["actions"]), agent.MAX_ACTIONS)


class AgentHandleTests(unittest.TestCase):
    def setUp(self):
        llm.CONVERSATION_HISTORY.clear()

    def tearDown(self):
        llm.CONVERSATION_HISTORY.clear()

    def test_runs_actions_in_order_and_reports_results(self):
        steps = [
            {"tool": "open_app", "args": {"name": "notepad"}},
            {"tool": "set_volume", "args": {"change": "down"}},
        ]
        with patch.object(llm, "complete", return_value=(plan(steps, "On it."), True)):
            with patch.object(tools, "execute", side_effect=["Opened Notepad.", "Turning the volume down."]) as run:
                response = agent.handle("open notepad and lower the volume")
        self.assertEqual([call.args[0] for call in run.call_args_list], ["open_app", "set_volume"])
        self.assertEqual(response, "Opened Notepad. Turning the volume down.")
        self.assertEqual(llm.CONVERSATION_HISTORY[-1]["content"], response)

    def test_stops_after_a_failed_step(self):
        steps = [{"tool": "open_app", "args": {"name": "x"}}, {"tool": "type_text", "args": {"text": "hi"}}]
        with patch.object(llm, "complete", return_value=(plan(steps), True)):
            with patch.object(tools, "execute", side_effect=tools.ToolError("no app called x")) as run:
                response = agent.handle("open x and type hi")
        run.assert_called_once()
        self.assertIn("no app called x", response)

    def test_chat_reply_without_actions(self):
        with patch.object(llm, "complete", return_value=(plan([], "Binary search halves the range each step."), True)):
            with patch.object(tools, "execute") as run:
                response = agent.handle("how does binary search work")
        run.assert_not_called()
        self.assertEqual(response, "Binary search halves the range each step.")

    def test_plain_text_model_output_is_used_as_reply(self):
        with patch.object(llm, "complete", return_value=("Paris is the capital of France.", True)):
            self.assertEqual(agent.handle("capital of france?"), "Paris is the capital of France.")

    def test_provider_failure_message_is_returned(self):
        with patch.object(llm, "complete", return_value=("I cannot reach the local language model.", False)):
            self.assertEqual(agent.handle("hello"), "I cannot reach the local language model.")
        self.assertEqual(llm.CONVERSATION_HISTORY, [])

    def test_power_action_needs_typed_confirmation_end_to_end(self):
        steps = [{"tool": "power_action", "args": {"action": "restart"}}]
        main._PENDING_ACTION = None
        try:
            with patch.object(llm, "complete", return_value=(plan(steps), True)):
                prompt, _ = main.handle_command("please reboot my machine")
            self.assertIn("Type CONFIRM", prompt)
            self.assertEqual(main._PENDING_ACTION.payload, {"action": "restart"})
            with patch.object(tools, "run_power_action", return_value="Restarting now.") as run_power:
                spoken, _ = main.handle_command("confirm", source="voice")
                run_power.assert_not_called()
                with patch.object(app, "_record_action", return_value=Path("actions.jsonl")):
                    done, _ = main.handle_command("confirm", source="typed")
            run_power.assert_called_once_with("restart")
            self.assertIn("type CONFIRM", spoken)
            self.assertIn("Restarting now.", done)
        finally:
            main._PENDING_ACTION = None


class AgentRobustnessTests(unittest.TestCase):
    def setUp(self):
        llm.CONVERSATION_HISTORY.clear()

    def test_missing_search_query_is_filled_from_the_request(self):
        steps = [{"tool": "web_search", "args": {}}]
        with patch.object(llm, "complete", return_value=(plan(steps), True)):
            with patch.object(tools, "_open_url") as open_url:
                agent.handle("jarvis I just want to know who is the most famous youtuber of all time?")
        self.assertIn("who%20is%20the%20most%20famous%20youtuber%20of%20all%20time", open_url.call_args.args[0])

    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "k", "OPENROUTER_API_KEY": "", "XAI_API_KEY": "", "JARVIS_CLOUD_PROVIDER": "auto"})
    def test_busy_gemini_is_retried_once_before_falling_back(self):
        busy = RuntimeError("Gemini API returned HTTP 503: high demand")
        with patch.object(llm, "is_internet_available", return_value=True), \
                patch.object(llm.time, "sleep"), \
                patch.object(llm, "_request_gemini", side_effect=[busy, ("m", "ok")]) as gemini, \
                patch.object(llm, "_request_ollama") as ollama:
            self.assertEqual(llm.complete([{"role": "user", "content": "hi"}], "SYS"), ("ok", True))
        self.assertEqual(gemini.call_count, 2)
        ollama.assert_not_called()


class ExecuteTests(unittest.TestCase):
    def test_unknown_tool(self):
        with self.assertRaises(tools.ToolError):
            tools.execute("format_hard_drive", {})

    def test_missing_required_argument(self):
        with self.assertRaisesRegex(tools.ToolError, "needs name"):
            tools.execute("open_app", {})

    def test_coerces_numbers_and_drops_unknown_args(self):
        with patch.dict(tools.TOOLS, {"set_volume": tools.Tool(lambda **kw: kw, "", tools.TOOLS["set_volume"].params)}):
            self.assertEqual(tools.execute("set_volume", {"level": "30", "rm": "-rf"}), {"level": 30})

    def test_unexpected_exception_becomes_tool_error(self):
        def boom():
            raise OSError("disk on fire")

        with patch.dict(tools.TOOLS, {"lock_screen": tools.Tool(boom, "")}):
            with self.assertRaisesRegex(tools.ToolError, "disk on fire"):
                tools.execute("lock_screen")


class HotkeyTests(unittest.TestCase):
    def test_parses_common_shortcuts(self):
        self.assertEqual(tools.parse_hotkey("ctrl+shift+s"), [0x11, 0x10, ord("S")])
        self.assertEqual(tools.parse_hotkey("Alt + Tab"), [0x12, 0x09])
        self.assertEqual(tools.parse_hotkey("windows+d"), [0x5B, ord("D")])
        self.assertEqual(tools.parse_hotkey("ctrl+page down"), [0x11, 0x22])

    def test_rejects_unknown_keys(self):
        with self.assertRaises(tools.ToolError):
            tools.parse_hotkey("ctrl+banana")


class WritingToolTests(unittest.TestCase):
    def test_write_in_notepad_generates_and_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tools, "IS_WINDOWS", False), \
                    patch.object(app, "get_desktop_directory", return_value=Path(folder)), \
                    patch.object(llm, "generate_text", return_value="The Sun is a star.") as generate:
                message = tools.write_in_notepad(write_about="a short essay about the sun", title="Sun essay")
            saved = Path(folder) / "Jarvis Notes" / "Sun_essay.txt"
            self.assertEqual(saved.read_text(encoding="utf-8").strip(), "The Sun is a star.")
        generate.assert_called_once_with("a short essay about the sun")
        self.assertIn("Sun_essay.txt", message)

    def test_write_then_read_back_after_restart_uses_real_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            desktop = Path(folder) / "OneDrive" / "Desktop"
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}), patch.object(tools, "IS_WINDOWS", False), \
                    patch.object(app, "get_desktop_directory", return_value=desktop), \
                    patch.object(llm, "generate_text", return_value="Earth is our home."):
                said = tools.write_in_notepad(write_about="an essay about Earth", title="Earth Essay", read_aloud=True)
                self.assertTrue((desktop / "Jarvis Notes" / "Earth_Essay.txt").is_file())
                self.assertIn("Here it is: Earth is our home.", said)
                app.LAST_CREATED_FILE = None  # simulate restarting Jarvis
                self.assertEqual(tools.read_last_written(), "Here's Earth Essay: Earth is our home.")
                self.assertIn("Earth is our home.", app.read_latest_note())

    def test_read_text_file_does_not_read_out_the_path(self):
        with patch.object(app, "read_user_text_file",
                          return_value="Contents of C:\\Users\\sarth\\OneDrive\\Desktop\\Jarvis Notes\\Earth_Essay_2.txt:\nEarth..."):
            self.assertEqual(tools.read_text_file("Earth_Essay_2.txt"), "Here's Earth Essay 2: Earth...")

    def test_spotify_liked_songs_and_search(self):
        with patch.object(tools, "IS_WINDOWS", True), patch.object(tools.os, "startfile", create=True) as start:
            self.assertIn("your Liked Songs", tools.spotify_play("my liked songs playlist"))
            self.assertEqual(start.call_args.args[0], "spotify:collection:tracks")
            tools.spotify_play("arijit singh on spotify")
            self.assertEqual(start.call_args.args[0], "spotify:search:arijit%20singh")

    def test_draft_email_uses_contacts_and_generated_text_and_never_sends(self):
        with tempfile.TemporaryDirectory() as folder:
            contacts = Path(folder) / "contacts.json"
            contacts.write_text(json.dumps({"Rahul": "rahul@example.com"}), encoding="utf-8")
            with patch.object(tools, "CONTACTS_FILE", contacts), \
                    patch.dict(os.environ, {"JARVIS_MAIL_PROVIDER": "gmail"}), \
                    patch.object(llm, "generate_text", return_value="Subject: Project update\n\nHi Rahul,\nIt's done.\nSarthak") as generate, \
                    patch.object(tools, "_open_url") as open_url:
                message = tools.draft_email(to="rahul", about="the project is finished")
        query = parse_qs(urlparse(open_url.call_args.args[0]).query)
        self.assertEqual(query["to"], ["rahul@example.com"])
        self.assertEqual(query["su"], ["Project update"])
        self.assertEqual(query["body"], ["Hi Rahul,\nIt's done.\nSarthak"])
        self.assertIn("the project is finished", generate.call_args.args[0])
        self.assertIn("press Send", message)

    def test_draft_email_with_unknown_person_asks_for_address(self):
        with patch.object(tools, "CONTACTS_FILE", Path("missing.json")), patch.object(tools, "_open_url") as open_url:
            message = tools.draft_email(to="Priya", subject="Hi", body="Hello!")
        self.assertEqual(parse_qs(urlparse(open_url.call_args.args[0]).query).get("to"), None)
        self.assertIn("don't have an address for Priya", message)

    def test_youtube_play_opens_top_video_newest_first_for_latest(self):
        page = '... "videoId":"abcDEF12345" ... "videoId":"zzzzzzzzzzz" ...'
        fake = unittest.mock.MagicMock()
        fake.__enter__.return_value.read.return_value = page.encode()
        with patch("urllib.request.urlopen", return_value=fake) as urlopen, patch.object(tools, "_open_url") as open_url:
            message = tools.youtube_play("mrbeast latest video")
        self.assertIn("sp=CAI%253D", urlopen.call_args.args[0].full_url)
        open_url.assert_called_once_with("https://www.youtube.com/watch?v=abcDEF12345")
        self.assertIn("Playing", message)

    def test_youtube_play_stops_reading_once_video_found(self):
        reads = []

        def read(size):
            reads.append(size)
            return b'x' * 1000 + b'"videoId":"abcDEF12345"' if len(reads) == 1 else b"y" * size

        fake = unittest.mock.MagicMock()
        fake.__enter__.return_value.read.side_effect = read
        with patch("urllib.request.urlopen", return_value=fake), patch.object(tools, "_open_url"):
            tools.youtube_play("gtsx most viewed")
        self.assertEqual(len(reads), 1)

    def test_youtube_play_falls_back_to_results_page(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), patch.object(tools, "_open_url") as open_url:
            tools.youtube_play("lofi music")
        self.assertIn("results?search_query=lofi%20music", open_url.call_args.args[0])

    def test_open_website_rejects_other_schemes(self):
        with patch.object(tools, "_open_url") as open_url:
            with self.assertRaises(tools.ToolError):
                tools.open_website("file:///C:/Windows")
            tools.open_website("example.com/docs")
        open_url.assert_called_once_with("https://example.com/docs")

    def test_close_app_refuses_protected_processes(self):
        with self.assertRaisesRegex(tools.ToolError, "won't close"):
            tools.close_app("explorer")


class WhatsAppAndPrivateBrowsingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.contacts = Path(self.folder.name) / "contacts.json"
        self.contacts.write_text(json.dumps({
            "Aadi": {"phone": "+91 98765-43210"},
            "Vedant Sharma": {"phone": "919999988888", "email": "vedant@example.com"},
            "Rahul": "rahul@example.com",
        }), encoding="utf-8")
        self.patcher = patch.object(tools, "CONTACTS_FILE", self.contacts)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.folder.cleanup()

    def test_contact_lookup_is_forgiving(self):
        self.assertEqual(tools.find_contact("adi", "phone"), ("aadi", "919876543210"))
        self.assertEqual(tools.find_contact("Vedant", "phone"), ("vedant sharma", "919999988888"))
        self.assertEqual(tools.find_contact("vedant", "email"), ("vedant sharma", "vedant@example.com"))
        self.assertEqual(tools.find_contact("rahul", "email"), ("rahul", "rahul@example.com"))
        self.assertEqual(tools.find_contact("priya", "phone"), (None, ""))

    def test_whatsapp_message_opens_chat_with_text(self):
        with patch.object(tools, "IS_WINDOWS", False), patch.object(tools, "_open_url") as open_url:
            message = tools.whatsapp_message("adi", text="hi")
        self.assertEqual(open_url.call_args.args[0], "https://web.whatsapp.com/send?phone=919876543210&text=hi")
        self.assertIn("Say 'send it'", message)

    def test_whatsapp_message_without_number_opens_contact_picker(self):
        with patch.object(tools, "IS_WINDOWS", False), patch.object(tools, "_open_url") as open_url:
            message = tools.whatsapp_message("Priya", text="see you at 5")
        self.assertEqual(open_url.call_args.args[0], "https://web.whatsapp.com/send?text=see%20you%20at%205")
        self.assertIn("Pick Priya from the list", message)

    def test_add_contact_saves_and_is_used_next_time(self):
        self.assertEqual(tools.add_contact("Priya", phone="98765 43210"), "Saved Priya's phone 919876543210.")
        tools.add_contact("rahul", phone="+44 7700 900123")  # keeps his existing email
        saved = json.loads(self.contacts.read_text(encoding="utf-8"))
        self.assertEqual(saved["priya"], {"phone": "919876543210"})
        self.assertEqual(saved["Rahul"], {"email": "rahul@example.com", "phone": "447700900123"})
        self.assertNotIn("rahul", saved)
        self.assertEqual(tools.find_contact("priya", "phone"), ("priya", "919876543210"))
        with self.assertRaises(tools.ToolError):
            tools.add_contact("bob", phone="12345")

    def test_close_tab_only_in_a_browser(self):
        with patch.object(tools, "IS_WINDOWS", True), patch.object(tools, "_leave_jarvis_window"), \
                patch.object(tools, "_press_combo") as press:
            with patch.object(tools, "foreground_window_title", return_value="MrBeast - YouTube - Google Chrome"):
                self.assertEqual(tools.close_tab(), "Closed the tab.")
            with patch.object(tools, "foreground_window_title", return_value="Document1 - Word"):
                with self.assertRaises(tools.ToolError):
                    tools.close_tab()
        press.assert_called_once_with([0x11, ord("W")])

    def test_whatsapp_message_needs_content(self):
        with self.assertRaises(tools.ToolError):
            tools.whatsapp_message("adi")

    def test_whatsapp_send_refuses_when_whatsapp_is_not_in_front(self):
        with patch.object(tools, "IS_WINDOWS", True), \
                patch.object(tools, "_leave_jarvis_window"), \
                patch.object(tools, "foreground_window_title", return_value="Notepad"), \
                patch.object(tools.time, "sleep"), \
                patch.object(tools, "_press_combo") as press:
            with self.assertRaisesRegex(tools.ToolError, "didn't send"):
                tools.whatsapp_send()
        press.assert_not_called()

    def test_private_search_goes_to_the_agent_not_the_quick_search(self):
        with patch("webbrowser.open") as browser:
            self.assertIsNone(app.trigger_action("search cat videos in a private browser"))
            self.assertIsNone(app.trigger_action("search mrbeast on youtube in incognito"))
        browser.assert_not_called()

    def test_terminate_closes_jarvis(self):
        main._PENDING_ACTION = None
        for text in ["terminate", "Exit.", "close jarvis"]:
            self.assertTrue(main.handle_command(text)[1], text)
        with patch.object(agent, "handle", return_value="Use kill."):
            self.assertFalse(main.handle_command("how do I terminate a process in linux")[1])


class QuickCommandRoutingTests(unittest.TestCase):
    """Quick commands must match the whole request; everything else goes to the agent."""

    def test_questions_and_compound_requests_are_not_hijacked(self):
        with patch("webbrowser.open") as browser:
            for text in [
                "how does binary search work",
                "open notepad and write an essay about the sun",
                "what time does the match start tomorrow",
                "what is google's revenue",
                "how long is my commute",
                "tell me about the meeting notes format",
                "draft a mail to rahul about the project",
            ]:
                self.assertIsNone(app.trigger_action(text), text)
        browser.assert_not_called()

    def test_simple_commands_still_work_instantly(self):
        with patch("webbrowser.open") as browser:
            self.assertIn("YouTube", app.trigger_action("Jarvis, open YouTube please"))
        browser.assert_called_once_with("https://www.youtube.com")
        with patch.object(tools, "set_volume", return_value="Turning the volume down.") as volume:
            self.assertEqual(app.trigger_action("lower the voice"), "Turning the volume down.")
            app.trigger_action("set the volume to 30 percent")
        self.assertEqual(volume.call_args_list[-1].kwargs, {"level": 30})
        self.assertIn("current time", app.trigger_action("what time is it"))

    def test_main_sends_unmatched_requests_to_agent(self):
        main._PENDING_ACTION = None
        with patch.object(agent, "handle", return_value="Writing your essay.") as handle:
            response, should_close = main.handle_command("open notepad and write an essay about the sun")
        handle.assert_called_once_with("open notepad and write an essay about the sun", "typed")
        self.assertEqual(response, "Writing your essay.")
        self.assertFalse(should_close)


class CompleteTests(unittest.TestCase):
    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "k", "OPENROUTER_API_KEY": "", "XAI_API_KEY": "", "JARVIS_CLOUD_PROVIDER": "auto"})
    def test_complete_passes_prompt_tokens_and_json_mode(self):
        with patch.object(llm, "is_internet_available", return_value=True), \
                patch.object(llm, "_request_gemini", return_value=("m", "{}")) as gemini:
            self.assertEqual(llm.complete([{"role": "user", "content": "hi"}], "SYS", 900, True), ("{}", True))
        gemini.assert_called_once_with([{"role": "user", "content": "hi"}], "SYS", 900, True)

    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "", "OPENROUTER_API_KEY": "", "XAI_API_KEY": ""})
    def test_generate_text_raises_when_no_model_is_reachable(self):
        with patch.object(llm, "_request_ollama", return_value=(None, "Ollama is down")):
            with self.assertRaisesRegex(RuntimeError, "Ollama is down"):
                llm.generate_text("write a poem")


if __name__ == "__main__":
    unittest.main()
