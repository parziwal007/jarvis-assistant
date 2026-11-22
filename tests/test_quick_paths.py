import unittest
from unittest.mock import patch

from automation import app, tools
from brain import llm


class QuickPathTests(unittest.TestCase):
    def test_open_unknown_app_skips_language_model(self):
        with patch.object(tools, "open_app", return_value="Opening Camera.") as opener:
            self.assertEqual(app.trigger_action("Jarvis open camera"), "Opening Camera.")
        opener.assert_called_once_with("camera")

    def test_open_whatsapp_is_instant(self):
        with patch.object(tools, "open_app", return_value="Opening WhatsApp.") as opener:
            self.assertEqual(app.trigger_action("open whatsapp"), "Opening WhatsApp.")
        opener.assert_called_once_with("whatsapp")

    def test_unknown_open_falls_back_to_agent(self):
        with patch.object(tools, "open_app", side_effect=tools.ToolError("nope")):
            self.assertIsNone(app.trigger_action("open the thing i mentioned"))

    def test_multi_step_open_goes_to_agent(self):
        with patch.object(tools, "open_app") as opener:
            self.assertIsNone(app.trigger_action("open notepad and write an essay about the sun"))
            self.assertIsNone(app.trigger_action("open the email from adi"))
            self.assertIsNone(app.trigger_action("open a new tab"))
        opener.assert_not_called()

    def test_play_on_youtube_is_instant(self):
        with patch.object(tools, "youtube_play", return_value="Playing.") as player:
            self.assertEqual(app.trigger_action("play Karan Aujla songs on YouTube"), "Playing.")
            app.trigger_action("play lofi from youtube")
        self.assertEqual(player.call_args_list[0].kwargs, {"query": "karan aujla songs"})
        self.assertEqual(player.call_args_list[1].kwargs, {"query": "lofi"})

    def test_plain_play_still_toggles_media(self):
        with patch.object(tools, "media_control", return_value="Toggled.") as media:
            self.assertEqual(app.trigger_action("pause the song"), "Toggled.")
        media.assert_called_once_with("play_pause")


class BusyProviderTests(unittest.TestCase):
    def test_busy_provider_switches_without_retry_when_another_exists(self):
        calls = []

        def busy():
            calls.append(1)
            raise RuntimeError("HTTP 503: overloaded")

        with self.assertRaises(RuntimeError):
            llm._with_retry(busy, "gemini", others_available=True)
        self.assertEqual(len(calls), 1)

    def test_busy_only_provider_retries_once(self):
        results = iter([RuntimeError("HTTP 503: overloaded"), ("m", "ok")])

        def flaky():
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(llm.time, "sleep"):
            self.assertEqual(llm._with_retry(flaky, "gemini"), ("m", "ok"))


if __name__ == "__main__":
    unittest.main()


class HalfCommandTests(unittest.TestCase):
    def test_half_command_asks_instead_of_opening_something(self):
        with patch.object(tools, "open_app") as opener:
            self.assertEqual(app.trigger_action("jarvis open my"), "Open what, Sarthak?")
            self.assertEqual(app.trigger_action("play the"), "Play what, Sarthak?")
        opener.assert_not_called()

    def test_short_names_never_match_apps_by_substring(self):
        apps = [{"Name": "MyASUS", "AppID": "x"}, {"Name": "Camera", "AppID": "y"}]
        with patch.object(tools, "_load_start_apps", return_value=apps):
            self.assertIsNone(tools._find_start_app("my"))
            self.assertEqual(tools._find_start_app("camera")["Name"], "Camera")
            self.assertEqual(tools._find_start_app("cam")["Name"], "Camera")


class GeminiBackupTests(unittest.TestCase):
    def test_busy_gemini_uses_backup_model(self):
        calls = []

        def fake(messages, system_prompt=None, max_tokens=None, json_mode=False, model=None):
            calls.append(model)
            if model is None:
                raise RuntimeError("HTTP 503: overloaded")
            return model, "hello"

        with patch.object(llm, "_request_gemini", side_effect=fake), \
                patch.dict("os.environ", {"JARVIS_GEMINI_BACKUP_MODEL": "gemini-flash-latest"}):
            self.assertEqual(llm._gemini_with_backup([], "", None, False, False), ("gemini-flash-latest", "hello"))
        self.assertEqual(calls, [None, "gemini-flash-latest"])

    def test_other_errors_are_not_retried(self):
        with patch.object(llm, "_request_gemini", side_effect=RuntimeError("HTTP 400: bad key")) as request:
            with self.assertRaises(RuntimeError):
                llm._gemini_with_backup([], "", None, False, False)
        self.assertEqual(request.call_count, 1)


class BarePlayTests(unittest.TestCase):
    def test_bare_play_still_toggles_media(self):
        with patch.object(tools, "media_control", return_value="Toggled.") as media:
            self.assertEqual(app.trigger_action("play"), "Toggled.")
        media.assert_called_once_with("play_pause")
