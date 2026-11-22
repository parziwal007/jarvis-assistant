"""Lets Jarvis read the code in a folder and explain it, review it or answer a question about it.

Only reads; it never changes your code. Secrets (.env files, keys, passwords files) are never read or sent.
"""
import datetime
import os
import re
from pathlib import Path

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css", ".scss", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".php", ".rb", ".kt", ".swift", ".sql", ".sh", ".ps1", ".bat", ".vue", ".svelte",
    ".dart", ".r", ".m", ".ipynb", ".json", ".yaml", ".yml", ".toml", ".xml", ".md", ".txt",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next", ".idea",
             ".vscode", "target", "bin", "obj", ".mypy_cache", ".pytest_cache", "site-packages", "coverage"}
SECRET_NAME = re.compile(r"(^\.env|secret|password|credential|token|\.pem$|\.key$|\.pfx$|id_rsa|contacts\.json$)",
                         re.IGNORECASE)
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "composer.lock"}
MAX_FILE_BYTES = 60_000
MAX_FILES = 60


class CodeReviewError(Exception):
    pass


def _norm(name):
    text = re.sub(r"\bdot\b", ".", str(name).casefold())  # "app dot js" -> "app.js"
    return re.sub(r"[\s_\-.]+", "", text)


def collect_files(folder, wanted_file=None):
    """The folder's readable code files, most useful first: a named file, README, then shallow, small files."""
    folder = Path(folder)
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIRS and not d.startswith(".")]
        depth = len(Path(root).relative_to(folder).parts)
        for name in files:
            path = Path(root) / name
            if path.suffix.casefold() not in CODE_EXTENSIONS or SECRET_NAME.search(name) or name in LOCK_FILES:
                continue
            if name.endswith((".min.js", ".min.css", ".map")):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if 0 < size <= MAX_FILE_BYTES:
                found.append((path, depth, size))
        if len(found) > 2000:
            break

    def rank(item):
        path, depth, size = item
        name = path.name.casefold()
        named = bool(wanted_file) and _norm(wanted_file) in _norm(name)
        is_readme = name.startswith("readme")
        is_data = path.suffix.casefold() in {".json", ".yaml", ".yml", ".toml", ".xml", ".txt"}
        return (not named, not is_readme, is_data, depth, size)

    found.sort(key=rank)
    return [path for path, _, _ in found[:MAX_FILES]]


def build_bundle(folder, files, budget):
    """Joins files into one text for the AI, within a character budget. Returns (text, files_used)."""
    parts, used, total = [], [], 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.casefold() == ".ipynb":
            text = text[:20_000]
        relative = path.relative_to(folder).as_posix()
        block = f"===== FILE: {relative} =====\n{text}\n"
        if total + len(block) > budget:
            if not used:  # always include something from the first file
                block = block[:budget]
            else:
                continue
        parts.append(block)
        used.append(relative)
        total += len(block)
        if total >= budget:
            break
    return "".join(parts), used


def review(folder, question=None, wanted_file=None, ask_ai=None, budget=None):
    """Asks the AI about the code. Returns (spoken_summary, full_markdown, files_used)."""
    folder = Path(folder)
    files = collect_files(folder, wanted_file)
    if not files:
        raise CodeReviewError(f"I couldn't find any code files in {folder.name}.")
    if wanted_file and _norm(wanted_file) not in _norm(files[0].name):
        raise CodeReviewError(f"I couldn't find a file called {wanted_file} in {folder.name}.")
    bundle, used = build_bundle(folder, files, budget or 100_000)
    task = question.strip() if question and question.strip() else (
        "Explain what this project does, how it is organised, and point out any bugs, security problems or "
        "clear improvements.")
    instruction = (
        f"You are reviewing the code in the folder '{folder.name}'. The user's request: {task}\n\n"
        "Answer in two parts, exactly in this format:\n"
        "SPOKEN: two or three plain sentences that Jarvis will say out loud (no code, no symbols, no file paths).\n"
        "DETAILS:\n"
        "a clear Markdown answer with headings and bullet points, naming the files and line snippets you refer "
        "to. If you suggest fixes, show the corrected code. Only describe problems you can actually see in the "
        "code; say so if you're unsure.\n\n"
        f"The files ({len(used)} of {len(files)} shown):\n\n{bundle}"
    )
    content = ask_ai(instruction)
    spoken, details = _split_answer(content)
    return spoken, details, used


def _split_answer(content):
    content = str(content or "").strip()
    match = re.search(r"SPOKEN:\s*(.+?)\s*DETAILS:\s*(.*)", content, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    first = re.split(r"(?<=[.!?])\s+", content)
    return " ".join(first[:2]).strip(), content


def save_report(folder, question, details, used, notes_dir):
    notes_dir = Path(notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(folder).name)[:40] or "code"
    path = notes_dir / f"Code_Review_{safe}_{stamp:%Y%m%d_%H%M%S}.md"
    header = (f"# Jarvis code review: {Path(folder).name}\n\n"
              f"*{stamp:%d %B %Y, %H:%M}* — folder: `{folder}`\n\n"
              f"**Request:** {question or 'General review'}\n\n"
              f"**Files read ({len(used)}):** " + ", ".join(f"`{u}`" for u in used) + "\n\n---\n\n")
    path.write_text(header + details + "\n", encoding="utf-8")
    return path
