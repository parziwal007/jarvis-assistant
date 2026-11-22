import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation import app, code_review, tools


class CodeReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "html"
        (self.project / "css").mkdir(parents=True)
        (self.project / "node_modules" / "lib").mkdir(parents=True)
        (self.project / "index.html").write_text("<h1>Hello</h1><script src='app.js'></script>")
        (self.project / "app.js").write_text("function add(a, b) { return a - b; }")
        (self.project / "css" / "style.css").write_text("h1 { color: red }")
        (self.project / "README.md").write_text("# My site")
        (self.project / ".env").write_text("API_KEY=supersecret")
        (self.project / "secrets.json").write_text('{"password": "x"}')
        (self.project / "node_modules" / "lib" / "big.js").write_text("junk")
        (self.project / "logo.png").write_bytes(b"\x89PNG")
        self.notes = self.root / "notes"

    def test_reads_code_but_never_secrets_or_dependencies(self):
        files = [p.name for p in code_review.collect_files(self.project)]
        self.assertEqual(files[0], "README.md")
        self.assertIn("app.js", files)
        self.assertIn("style.css", files)
        for hidden in (".env", "secrets.json", "big.js", "logo.png"):
            self.assertNotIn(hidden, files)

    def test_named_file_comes_first(self):
        self.assertEqual(code_review.collect_files(self.project, "app js")[0].name, "app.js")

    def test_review_splits_spoken_and_details_and_sends_no_secret(self):
        sent = []

        def fake_ai(text):
            sent.append(text)
            return "SPOKEN: It's a tiny website. The add function subtracts by mistake.\nDETAILS:\n## Bugs\n- app.js"

        spoken, details, used = code_review.review(self.project, "find bugs", None, fake_ai)
        self.assertEqual(spoken, "It's a tiny website. The add function subtracts by mistake.")
        self.assertTrue(details.startswith("## Bugs"))
        self.assertIn("app.js", used)
        self.assertNotIn("supersecret", sent[0])
        self.assertIn("find bugs", sent[0])

    def test_budget_limits_how_much_is_sent(self):
        _, used = code_review.build_bundle(self.project, code_review.collect_files(self.project), 120)
        self.assertGreaterEqual(len(used), 1)
        self.assertLess(len(used), 4)

    def test_tool_uses_the_folder_just_opened(self):
        tools._set_current_folder(self.project)
        self.addCleanup(setattr, tools, "_current_folder", None)
        answer = "SPOKEN: Looks fine.\nDETAILS:\nAll good."
        with patch("brain.llm.generate_text", return_value=answer), \
                patch("brain.llm.get_cloud_provider_candidates", return_value=["gemini"]), \
                patch.object(app, "notes_directory", return_value=self.notes), \
                patch.object(app, "remember_last_file"), patch.object(tools, "IS_WINDOWS", False):
            reply = tools.review_code("what does this do")
        self.assertTrue(reply.startswith("Looks fine. I read 4 files"))
        reports = list(self.notes.glob("Code_Review_html_*.md"))
        self.assertEqual(len(reports), 1)
        self.assertIn("All good.", reports[0].read_text())

    def test_no_folder_open_asks_which(self):
        tools._current_folder = None
        with self.assertRaises(tools.ToolError):
            tools.review_code()

    def test_missing_named_file(self):
        with self.assertRaises(code_review.CodeReviewError):
            code_review.review(self.project, None, "server.py", lambda text: "")


if __name__ == "__main__":
    unittest.main()
