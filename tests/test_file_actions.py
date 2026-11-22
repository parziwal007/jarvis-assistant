import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from automation import app


class FileActionTests(unittest.TestCase):
    def setUp(self):
        app.LAST_CREATED_FILE = None
        main._PENDING_ACTION = None
        state_folder = tempfile.TemporaryDirectory()  # keep "last written file" state out of the real profile
        self.addCleanup(state_folder.cleanup)
        env = patch.dict("os.environ", {"LOCALAPPDATA": state_folder.name})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        app.LAST_CREATED_FILE = None
        main._PENDING_ACTION = None

    def test_parses_textfile_essay_request(self):
        request = app.parse_text_file_request(
            "create textfile for me and write an essay in it about sun"
        )

        self.assertEqual(request["topic"], "sun")
        self.assertEqual(request["filename"], "sun_Essay.txt")
        self.assertEqual(request["content"], "")

    def test_parses_the_users_simple_textfile_wording(self):
        request = app.parse_text_file_request(
            "create a simple textfile and in that file write an essay about sun"
        )

        self.assertEqual(request["topic"], "sun")
        self.assertEqual(request["filename"], "sun_Essay.txt")

    def test_generates_and_saves_requested_essay_on_desktop(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            desktop = Path(temporary_directory)
            with patch.object(app, "get_desktop_directory", return_value=desktop):
                with patch.object(main.llm, "brain", return_value="The Sun is a star.") as generate:
                    response, should_exit = main.handle_command(
                        "create a simple textfile and in that file write an essay about sun"
                    )

            essay_file = desktop / "sun_Essay.txt"
            self.assertTrue(essay_file.is_file())
            self.assertEqual(essay_file.read_text(encoding="utf-8").strip(), "The Sun is a star.")
            self.assertIn(str(essay_file), response)
            self.assertFalse(should_exit)
            generate.assert_called_once()

    def test_open_it_opens_latest_created_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "get_desktop_directory", return_value=Path(temporary_directory)):
                file_path = app.create_desktop_text_file("Sun_Essay.txt", "The Sun is a star.")
            with patch.object(app, "_open_notepad_with_ui") as open_file:
                response = app.trigger_action("where is the sun textfile open it")

        self.assertIn(str(file_path), response)
        open_file.assert_called_once_with(file_path)

    def test_explicit_content_file_request_saves_without_llm(self):
        request = app.parse_text_file_request(
            "create text file named greeting.txt with content Hello there."
        )

        self.assertEqual(request["filename"], "greeting.txt")
        self.assertEqual(request["content"], "Hello there.")

    def test_find_and_read_file_are_limited_to_user_folders(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            documents = Path(temporary_directory) / "Documents"
            documents.mkdir()
            note = documents / "project-note.txt"
            note.write_text("Jarvis file actions work.", encoding="utf-8")
            folders = {
                "desktop": Path(temporary_directory) / "Desktop",
                "documents": documents,
                "downloads": Path(temporary_directory) / "Downloads",
            }
            with patch.object(app, "get_user_folders", return_value=folders):
                matches, truncated = app.find_user_files("project-note.txt")
                contents = app.read_user_text_file("project-note.txt")
                outside_result = app.trigger_action("read file C:\\Windows\\win.ini")

        self.assertEqual(matches, [note])
        self.assertFalse(truncated)
        self.assertIn("Jarvis file actions work.", contents)
        self.assertIn("could not read", outside_result.lower())

    def test_file_replacement_requires_typed_confirmation_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            documents = Path(temporary_directory) / "Documents"
            documents.mkdir()
            note = documents / "draft.txt"
            note.write_text("Original text.", encoding="utf-8")
            folders = {
                "desktop": Path(temporary_directory) / "Desktop",
                "documents": documents,
                "downloads": Path(temporary_directory) / "Downloads",
            }
            with patch.object(app, "get_user_folders", return_value=folders):
                response, _ = main.handle_command("replace file draft.txt with Revised text.")
                voice_confirmation, _ = main.handle_command("confirm", source="voice")
                self.assertIn("Original text.", note.read_text(encoding="utf-8"))
                with patch.object(app, "_record_action", return_value=Path(temporary_directory) / "actions.jsonl"):
                    completion, _ = main.handle_command("confirm", source="typed")

            backup = documents / "draft.txt.jarvis-backup"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), "Original text.")
            self.assertEqual(note.read_text(encoding="utf-8"), "Revised text.")

        self.assertIn("Type CONFIRM", response)
        self.assertIn("type CONFIRM", voice_confirmation)
        self.assertIn("backup", completion.lower())

    def test_download_organization_shows_preview_and_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            downloads = Path(temporary_directory) / "Downloads"
            downloads.mkdir()
            image = downloads / "photo.jpg"
            image.write_bytes(b"image")
            folders = {
                "desktop": Path(temporary_directory) / "Desktop",
                "documents": Path(temporary_directory) / "Documents",
                "downloads": downloads,
            }
            with patch.object(app, "get_user_folders", return_value=folders):
                preview, _ = main.handle_command("organize downloads by file type")
                self.assertTrue(image.exists())
                with patch.object(app, "_record_action", return_value=Path(temporary_directory) / "actions.jsonl"):
                    completion, _ = main.handle_command("CONFIRM")
                self.assertTrue((downloads / "Images" / "photo.jpg").is_file())

        self.assertIn("photo.jpg -> Images", preview)
        self.assertIn("Organized 1 file", completion)

    def test_shutdown_requires_typed_confirmation(self):
        with patch.object(app, "_record_action", return_value=Path("actions.jsonl")):
            with patch.object(app, "shutdown_laptop") as shutdown:
                preview, _ = main.handle_command("shutdown laptop")
                voice_confirmation, _ = main.handle_command("confirm", source="voice")
                shutdown.assert_not_called()
                main.handle_command("confirm", source="typed")

        self.assertIn("Type CONFIRM", preview)
        self.assertIn("type CONFIRM", voice_confirmation)
        shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
