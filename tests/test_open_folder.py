import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import app, tools


class OpenFolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        for folder in [
            "Desktop/Manga Project/chapters",
            "Downloads/Anime/Solo_Leveling",
            "Documents/College/Sem 5/DBMS notes",
            "Documents/node_modules/solo leveling",  # skipped folder
            "Pictures/Solo Leveling Wallpapers",
        ]:
            (self.home / folder).mkdir(parents=True)
        for target, value in [(tools, "_folder_search_roots"), ]:
            pass
        roots = patch.object(tools, "_folder_search_roots",
                             return_value=[self.home / n for n in ("Desktop", "Documents", "Downloads", "Pictures")])
        roots.start()
        self.addCleanup(roots.stop)
        shown = patch.object(tools, "_show_folder")
        self.shown = shown.start()
        self.addCleanup(shown.stop)

    def test_opens_folder_by_spoken_name(self):
        reply = tools.open_folder("solo leveling folder")
        self.assertEqual(self.shown.call_args.args[0], self.home / "Downloads/Anime/Solo_Leveling")
        self.assertIn("Solo_Leveling", reply)

    def test_exact_name_beats_longer_name(self):
        tools.open_folder("solo leveling")
        self.assertEqual(self.shown.call_args.args[0].name, "Solo_Leveling")

    def test_finds_nested_and_multiword(self):
        tools.open_folder("manga project")
        self.assertEqual(self.shown.call_args.args[0].name, "Manga Project")
        tools.open_folder("dbms notes")
        self.assertEqual(self.shown.call_args.args[0].name, "DBMS notes")

    def test_unknown_folder_raises(self):
        with self.assertRaises(tools.ToolError):
            tools.open_folder("does not exist anywhere")
        self.shown.assert_not_called()

    def test_standard_folder_still_works(self):
        with patch.object(tools, "_standard_folders", return_value={"downloads": self.home / "Downloads"}):
            self.assertEqual(tools.open_folder("my downloads folder"), "Opening your downloads folder.")

    def test_voice_command_opens_folder_without_language_model(self):
        with patch.object(tools, "open_app", side_effect=tools.ToolError("no app")):
            reply = app.trigger_action("Jarvis open solo leveling folder")
            self.assertIn("Solo_Leveling", reply)
            reply = app.trigger_action("open manga project")
            self.assertIn("Manga Project", reply)


if __name__ == "__main__":
    unittest.main()


class CreateFolderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / "Desktop").mkdir()
        (self.home / "Documents" / "College").mkdir(parents=True)
        for name, value in [
            ("_standard_folders", {"desktop": self.home / "Desktop", "documents": self.home / "Documents"}),
            ("_folder_search_roots", [self.home / "Desktop", self.home / "Documents"]),
        ]:
            patcher = patch.object(tools, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        desktop = patch.object(app, "get_desktop_directory", return_value=self.home / "Desktop")
        desktop.start()
        self.addCleanup(desktop.stop)
        shown = patch.object(tools, "_show_folder")
        self.shown = shown.start()
        self.addCleanup(shown.stop)

    def test_creates_on_desktop_by_default(self):
        self.assertEqual(tools.create_folder("Capstone"), "Created the Capstone folder in Desktop.")
        self.assertTrue((self.home / "Desktop" / "Capstone").is_dir())

    def test_creates_inside_a_named_folder(self):
        tools.create_folder("Sem 5", "my college folder")
        self.assertTrue((self.home / "Documents" / "College" / "Sem 5").is_dir())

    def test_existing_folder_is_opened_not_duplicated(self):
        tools.create_folder("Capstone")
        self.assertIn("already exists", tools.create_folder("capstone"))

    def test_strips_characters_windows_forbids(self):
        tools.create_folder('notes: "draft"?')
        self.assertTrue((self.home / "Desktop" / "notes draft").is_dir())

    def test_voice_command_creates_folder_instantly(self):
        reply = app.trigger_action("Jarvis create a folder called capstone on desktop")
        self.assertEqual(reply, "Created the capstone folder in Desktop.")

    def test_unnamed_request_goes_to_agent(self):
        self.assertIsNone(app.trigger_action("jarvis create a folder by the way"))
        self.assertFalse(any((self.home / "Desktop").iterdir()))


class VolumeAndYoutubeTests(unittest.TestCase):
    def test_increase_volume_by_percent_is_relative(self):
        with patch.object(tools, "set_volume", return_value="ok") as volume:
            app.trigger_action("jarvis increase the volume by 80%")
        volume.assert_called_once_with(change="up", amount=80)

    def test_most_liked_sorts_by_views(self):
        import io
        page = io.BytesIO(b'"videoId":"abcdefghijk"')
        with patch("urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value = page
            self.assertEqual(tools.find_youtube_video("gta 6 most liked"), "abcdefghijk")
        url = opener.call_args.args[0].full_url
        self.assertIn("sp=CAM%253D", url)
        self.assertIn("search_query=gta%206", url)
        self.assertNotIn("liked", url)


class OpenInAppTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / "Desktop" / "html").mkdir(parents=True)
        venv = self.home / "Desktop" / "project" / ".venv2"
        (venv / "Lib" / "html").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = x")
        for name, value in [("_folder_search_roots", [self.home / "Desktop"]),
                            ("_standard_folders", {"desktop": self.home / "Desktop"})]:
            patcher = patch.object(tools, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(app, "get_desktop_directory", return_value=self.home / "Desktop")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in [("IS_WINDOWS", True), ("NO_WINDOW", 0)]:
            patcher = patch.object(tools, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.popen = patch.object(tools.subprocess, "Popen").start()
        self.addCleanup(patch.stopall)
        tools._current_folder = None

    def test_python_install_folders_are_ignored(self):
        self.assertEqual(tools.find_folders("html"), [self.home / "Desktop" / "html"])

    def test_open_folder_in_vs_code(self):
        with patch.object(tools, "_find_vscode", return_value=["Code.exe"]):
            reply = app.trigger_action("open my html folder through vs code")
        self.assertEqual(reply, "Opening the html folder from Desktop in VS Code.")
        self.assertEqual(self.popen.call_args.args[0], ["Code.exe", str(self.home / "Desktop" / "html")])

    def test_visual_studio_means_vs_code_and_that_folder_works(self):
        with patch.object(tools, "_show_folder"):
            tools.open_folder("html")
        with patch.object(tools, "_find_vscode", return_value=["Code.exe"]):
            reply = tools.open_folder("that folder", app="visual studio")
        self.assertIn("in VS Code", reply)

    def test_long_spoken_request(self):
        with patch.object(tools, "_find_vscode", return_value=["Code.exe"]):
            reply = app.trigger_action("i want you to open my html folder in visual studio can you")
        self.assertIn("html folder", reply)


class OpenInAppPhrasingTests(unittest.TestCase):
    def test_other_ways_of_saying_it(self):
        cases = {
            "now in vs code open a folder name html": ("html", "vs code"),
            "in visual studio open the folder called html": ("html", "visual studio"),
            "open a folder named html in vs code": ("html", "vs code"),
            "jarvis i want you to open my html folder through vs code": ("html", "vs code"),
        }
        for said, expected in cases.items():
            with patch.object(tools, "open_folder", return_value="ok") as opener:
                self.assertEqual(app.trigger_action(said), "ok", said)
            self.assertEqual((opener.call_args.args[0], opener.call_args.kwargs["app"]), expected, said)
