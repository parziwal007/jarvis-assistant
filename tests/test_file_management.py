import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import app, tools


class FileManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.desktop = self.home / "Desktop"
        self.documents = self.home / "Documents"
        (self.desktop / "Capstone").mkdir(parents=True)
        (self.documents / "College").mkdir(parents=True)
        (self.desktop / "Capstone" / "report.pdf").write_text("x")
        (self.documents / "sun essay.txt").write_text("The sun.")
        roots = [self.desktop, self.documents]
        standard = {"desktop": self.desktop, "documents": self.documents}
        for target, name, value in [
            (tools, "_folder_search_roots", roots),
            (tools, "_standard_folders", standard),
            (app, "get_desktop_directory", self.desktop),
            (app, "notes_directory", self.desktop / "Jarvis Notes"),
        ]:
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_show_folder",):
            patcher = patch.object(tools, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"LOCALAPPDATA": str(self.home / "state")})
        env.start()
        self.addCleanup(env.stop)
        recycle = patch.object(tools, "_send_to_recycle_bin", side_effect=self._fake_recycle)
        self.recycled = []
        recycle.start()
        self.addCleanup(recycle.stop)
        tools._current_folder = None
        tools._pending_delete.clear()
        app.LAST_CREATED_FILE = None
        self.addCleanup(setattr, tools, "_current_folder", None)
        self.addCleanup(tools._pending_delete.clear)

    def _fake_recycle(self, path):
        import shutil
        self.recycled.append(path)
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    # --- saving into folders ---
    def test_write_essay_into_named_folder(self):
        with patch.object(tools, "_write", return_value="Essay text"), patch.object(tools.subprocess, "Popen"):
            reply = tools.write_in_notepad(write_about="an essay on the sun", title="Sun essay", folder="capstone")
        self.assertTrue((self.desktop / "Capstone" / "Sun_essay.txt").is_file())
        self.assertIn("Capstone folder", reply)

    def test_that_folder_means_the_one_just_created(self):
        tools.create_folder("Notes 2026")
        with patch.object(tools, "_write", return_value="hello"), patch.object(tools.subprocess, "Popen"):
            tools.write_in_notepad(text="hello", title="todo", folder="that folder")
        self.assertTrue((self.desktop / "Notes 2026" / "todo.txt").is_file())

    def test_move_file_into_folder(self):
        reply = tools.save_to_folder("sun essay", "capstone")
        self.assertTrue((self.desktop / "Capstone" / "sun essay.txt").is_file())
        self.assertFalse((self.documents / "sun essay.txt").exists())
        self.assertIn("Moved sun essay.txt", reply)

    def test_copy_keeps_original_and_avoids_overwrite(self):
        tools.save_to_folder("report dot pdf", "college", copy=True)
        tools.save_to_folder("report.pdf", "college", copy=True)
        self.assertTrue((self.desktop / "Capstone" / "report.pdf").is_file())
        self.assertTrue((self.documents / "College" / "report.pdf").is_file())
        self.assertTrue((self.documents / "College" / "report (2).pdf").is_file())

    def test_move_what_jarvis_just_wrote(self):
        with patch.object(tools, "_write", return_value="text"), patch.object(tools.subprocess, "Popen"):
            tools.write_in_notepad(text="text", title="Plan")
        tools.save_to_folder("it", "capstone")
        self.assertTrue((self.desktop / "Capstone" / "Plan.txt").is_file())
        self.assertEqual(app.last_written_file(), self.desktop / "Capstone" / "Plan.txt")

    # --- deleting ---
    def test_delete_asks_first_then_recycles(self):
        question = tools.delete_item("capstone", "folder")
        self.assertIn("Capstone folder in Desktop, with 1 file inside", question)
        self.assertTrue((self.desktop / "Capstone").is_dir())  # nothing deleted yet
        self.assertEqual(app.trigger_action("yes, delete it"), "Deleted Capstone. It's in the Recycle Bin if you need it back.")
        self.assertFalse((self.desktop / "Capstone").exists())

    def test_cancel_keeps_the_file(self):
        tools.delete_item("sun essay", "file")
        self.assertEqual(app.trigger_action("cancel"), "Okay, I won't delete it.")
        self.assertIsNone(app.trigger_action("yes delete it"))  # no question waiting: goes to the agent
        self.assertTrue((self.documents / "sun essay.txt").is_file())

    def test_yes_delete_does_nothing_without_a_question(self):
        with self.assertRaises(tools.ToolError):
            tools.confirm_delete()
        self.assertEqual(self.recycled, [])

    def test_confirmation_expires(self):
        tools.delete_item("sun essay", "file")
        tools._pending_delete["at"] -= tools.DELETE_CONFIRM_SECONDS + 1
        with self.assertRaises(tools.ToolError):
            tools.confirm_delete()
        self.assertTrue((self.documents / "sun essay.txt").is_file())

    def test_refuses_main_folders(self):
        with self.assertRaises(tools.ToolError):
            tools._check_safe_to_change(self.desktop)
        with self.assertRaises(tools.ToolError):
            tools._check_safe_to_change(tools.PROJECT_ROOT)

    def test_ambiguous_names_ask_which_one(self):
        (self.documents / "Capstone").mkdir()
        with self.assertRaises(tools.ToolError) as error:
            tools.delete_item("capstone", "folder")
        self.assertIn("Tell me which one", str(error.exception))
        self.assertIn("Should I delete", tools.delete_item("capstone", "folder", folder="documents"))

    def test_delete_that_folder(self):
        tools.create_folder("Temp stuff")
        self.assertIn("Temp stuff folder", tools.delete_item("that folder", "folder"))


if __name__ == "__main__":
    unittest.main()
