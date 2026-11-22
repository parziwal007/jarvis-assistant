import datetime
import hashlib
import json
import logging
import os
import psutil
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
import webbrowser

FILE_SIGNATURE_BASE = {}
LAST_CREATED_FILE = None
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfirmationRequest:
    action: str
    payload: dict
    prompt: str


def compute_sha256(file_path):
    """Calculates true SHA-256 checksums to verify file security."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception:
        return None


def run_security_scan(target_directory):
    """Scans files, maps their security hashes, and checks for structural safety."""
    global FILE_SIGNATURE_BASE
    if not os.path.exists(target_directory):
        return "Specified system directory sector cannot be located, Sarthak."

    scanned_count = 0
    anomalies = 0

    for root, _, files in os.walk(target_directory):
        for file in files:
            if "__pycache__" in root or ".git" in root:
                continue
            full_path = os.path.join(root, file)
            current_hash = compute_sha256(full_path)

            if current_hash:
                scanned_count += 1
                if full_path in FILE_SIGNATURE_BASE:
                    if FILE_SIGNATURE_BASE[full_path] != current_hash:
                        anomalies += 1
                else:
                    FILE_SIGNATURE_BASE[full_path] = current_hash

    if anomalies > 0:
        return f"Warning Sarthak! Security sweep complete. Detected {anomalies} unauthorized modifications inside your environment."
    return f"Security array sweep complete, Sarthak. Verified {scanned_count} distinct local files. All signatures match baseline perfectly."


def get_hardware_metrics():
    """Extracts raw processing loads and power parameters from your hardware layers."""
    cpu_load = psutil.cpu_percent(interval=0.1)
    ram_metrics = psutil.virtual_memory()
    battery = psutil.sensors_battery()
    battery_status = f"{battery.percent} percent" if battery else "Unknown"

    return (
        f"Mainframe CPU core utilization is at {cpu_load} percent. "
        f"System RAM resource utilization is at {ram_metrics.percent} percent. "
        f"Primary battery cell capacity is holding at {battery_status}."
    )


def get_current_time_report():
    """Returns the local time and date in a natural format."""
    now = datetime.datetime.now()
    return f"The current time is {now.strftime('%I:%M %p')} and the date is {now.strftime('%A, %B %d, %Y')}."


def open_project_folder():
    """Opens the project directory in File Explorer."""
    project_root = Path(__file__).resolve().parents[1]
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(project_root))
        else:
            subprocess.Popen(["xdg-open", str(project_root)])
        return f"Opening your project folder at {project_root}."
    except Exception as exc:
        return f"I could not open the project folder: {exc}"


def _open_notepad_with_ui(path=None):
    from automation.ui import open_in_notepad

    open_in_notepad(path)


def open_desktop_app(app_name):
    """Opens common Windows apps by name, including app paths when not on PATH."""
    app_map = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "code": "code",
        "vscode": "code",
        "visual studio code": "code",
        "notepad": "notepad",
        "calculator": "calc",
        "paint": "mspaint",
        "cmd": "cmd",
        "terminal": "cmd",
        "file explorer": "explorer",
        "explorer": "explorer",
    }

    target = app_map.get(app_name.strip().lower())
    if not target:
        return None

    executable = shutil.which(target) if target not in ["explorer", "cmd"] else shutil.which(target)
    if not executable:
        candidates = []
        if target in ["chrome", "msedge", "code"]:
            base_dir = Path.home() / "AppData" / "Local" / "Programs"
            if target == "chrome":
                candidates.append(base_dir / "Google" / "Chrome" / "Application" / "chrome.exe")
            elif target == "msedge":
                candidates.extend([
                    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                ])
            else:
                candidates.append(base_dir / "Microsoft VS Code" / "Code.exe")
        elif target in ["notepad", "calc", "mspaint", "explorer", "cmd"]:
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            if target == "notepad":
                candidates.append(system_root / "System32" / "notepad.exe")
            elif target == "calc":
                candidates.append(system_root / "System32" / "calc.exe")
            elif target == "mspaint":
                candidates.append(system_root / "System32" / "mspaint.exe")
            elif target == "explorer":
                candidates.append(system_root / "explorer.exe")
            elif target == "cmd":
                candidates.append(system_root / "System32" / "cmd.exe")

        for candidate in candidates:
            if candidate.exists():
                executable = str(candidate)
                break

    try:
        if target == "notepad" and os.name == "nt":
            try:
                _open_notepad_with_ui()
            except Exception:
                subprocess.Popen([executable or "notepad.exe"])
            return "Opening Notepad for you, Sarthak."
        if not executable:
            if target == "chrome":
                webbrowser.open("https://www.google.com")
                return "Chrome isn’t installed on this machine, so I opened your default browser to Google, Sarthak."
            if target == "code":
                return "VS Code is not installed on this machine, Sarthak."
            if target == "msedge":
                webbrowser.open("https://www.microsoft.com/edge")
                return "I could not find Edge, so I opened its download page in your default browser."
            if target == "explorer":
                subprocess.Popen(["explorer", "."])
                return "Opening File Explorer for you, Sarthak."
            if target == "notepad":
                subprocess.Popen(["notepad.exe"])
                return "Opening Notepad for you, Sarthak."
            if target == "calc":
                subprocess.Popen(["calc.exe"])
                return "Opening Calculator for you, Sarthak."
            if target == "mspaint":
                subprocess.Popen(["mspaint.exe"])
                return "Opening Paint for you, Sarthak."
            if target == "cmd":
                subprocess.Popen(["cmd.exe"])
                return "Opening the terminal for you, Sarthak."
            return f"I could not find an installed app for {app_name}."

        if target == "explorer":
            subprocess.Popen([executable, "."])
        else:
            subprocess.Popen([executable])
        return f"Opening {app_name} for you, Sarthak."
    except Exception as exc:
        return f"I hit a snag opening {app_name}: {exc}"


def notes_directory():
    """Desktop\\Jarvis Notes on the real Desktop (which may be inside OneDrive)."""
    return get_desktop_directory() / "Jarvis Notes"


def remember_last_file(path):
    """Remembers the last file Jarvis wrote, across restarts."""
    global LAST_CREATED_FILE
    LAST_CREATED_FILE = Path(path)
    try:
        state = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".cache") / "JarvisAssistant" / "last_file.txt"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(str(path), encoding="utf-8")
    except OSError:
        pass


def last_written_file():
    if LAST_CREATED_FILE is not None and Path(LAST_CREATED_FILE).is_file():
        return Path(LAST_CREATED_FILE)
    state = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".cache") / "JarvisAssistant" / "last_file.txt"
    try:
        path = Path(state.read_text(encoding="utf-8").strip())
        if path.is_file():
            return path
    except OSError:
        pass
    notes = [p for p in notes_directory().glob("*.txt") if p.name != "todo.txt"]
    return max(notes, key=lambda p: p.stat().st_mtime) if notes else None


def create_note_file(text_content):
    """Stores a quick note in the Desktop Jarvis Notes folder."""
    if not text_content or len(text_content.strip()) == 0:
        return "I didn’t find any note content to save, Sarthak."

    notes_dir = notes_directory()
    notes_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = notes_dir / f"note_{timestamp}.txt"
    file_path.write_text(text_content.strip(), encoding="utf-8")
    return f"Saved your note to {file_path}."


def read_latest_note():
    """Reads the most recently saved Jarvis note."""
    notes_dir = notes_directory()
    note_files = [path for path in notes_dir.glob("*.txt") if path.name != "todo.txt"]
    if not note_files:
        return "There are no saved notes yet, Sarthak."
    latest_note = max(note_files, key=lambda path: path.stat().st_mtime)
    note_text = latest_note.read_text(encoding="utf-8").strip()
    if not note_text:
        return "Your latest note is empty."
    return f"Your latest note says: {note_text}"


def read_todo_tasks():
    """Reads the saved task list."""
    task_file = notes_directory() / "todo.txt"
    if not task_file.exists():
        return "You do not have any saved tasks yet, Sarthak."
    tasks = task_file.read_text(encoding="utf-8").strip()
    return f"Your saved tasks are: {tasks}" if tasks else "Your task list is empty."


def open_user_folder(folder_name):
    """Opens one of the user's standard folders."""
    folders = {
        "desktop": get_desktop_directory(),
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
    }
    folder = folders.get(folder_name)
    if not folder:
        return None
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return f"Opening your {folder_name} folder."
    except Exception as exc:
        return f"I could not open your {folder_name} folder: {exc}"


def get_desktop_directory():
    if os.name == "nt":
        try:
            import ctypes

            path_buffer = ctypes.create_unicode_buffer(260)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, path_buffer)
            if result == 0 and path_buffer.value:
                return Path(path_buffer.value)
        except (AttributeError, OSError):
            pass
    return Path.home() / "Desktop"


def get_user_folders():
    return {
        "desktop": get_desktop_directory(),
        "documents": Path.home() / "Documents",
        "downloads": Path.home() / "Downloads",
    }


def _is_within_user_folders(path):
    resolved_path = path.resolve()
    for folder in get_user_folders().values():
        try:
            resolved_path.relative_to(folder.resolve())
            return True
        except ValueError:
            continue
    return False


def find_user_files(query, limit=10):
    needle = Path(query.strip().strip("\"'")).name.casefold()
    if not needle:
        return [], False

    matches = []
    inspected = 0
    truncated = False
    ignored_directories = {".git", ".venv", "node_modules", "__pycache__"}

    for folder in get_user_folders().values():
        if not folder.is_dir():
            continue
        for root, directories, filenames in os.walk(folder):
            directories[:] = [
                name for name in directories
                if name.casefold() not in ignored_directories and not name.startswith(".")
            ]
            for filename in filenames:
                inspected += 1
                if needle in filename.casefold():
                    matches.append(Path(root) / filename)
                    if len(matches) >= limit:
                        return matches, True
                if inspected >= 5000:
                    truncated = True
                    return matches, truncated
    return matches, truncated


def _resolve_user_file(query):
    matches, _ = find_user_files(query, limit=50)
    exact_matches = [
        path for path in matches
        if path.name.casefold() == Path(query.strip().strip("\"'")).name.casefold()
    ]
    candidates = exact_matches or matches
    if not candidates:
        raise FileNotFoundError(f"I could not find '{query}' in Desktop, Documents, or Downloads.")
    if len(candidates) != 1:
        locations = ", ".join(str(path) for path in candidates[:5])
        raise ValueError(f"That name is ambiguous. Matches: {locations}")
    path = candidates[0]
    if not _is_within_user_folders(path) or not path.is_file():
        raise ValueError("That file is outside the folders Jarvis is allowed to manage.")
    return path


def read_user_text_file(query):
    path = _resolve_user_file(query)
    if path.suffix.casefold() not in {".txt", ".md", ".csv", ".log", ".json"}:
        raise ValueError("For safety, Jarvis can read only plain-text files.")
    if path.stat().st_size > 1_000_000:
        raise ValueError("That file is too large to read safely (limit: 1 MB).")
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) > 4000:
        content = content[:4000] + "\n[Output truncated at 4,000 characters.]"
    return f"Contents of {path}:\n{content}"


def _action_log_path():
    base_directory = os.getenv("LOCALAPPDATA")
    if base_directory:
        return Path(base_directory) / "JarvisAssistant" / "actions.jsonl"
    return Path.home() / ".cache" / "jarvis_assistant" / "actions.jsonl"


def _record_action(action, target, status):
    log_path = _action_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "action": action,
        "target": str(target),
        "status": status,
    }
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def _unique_destination(path):
    destination = path
    index = 2
    while destination.exists():
        destination = path.with_name(f"{path.stem}_{index}{path.suffix}")
        index += 1
    return destination


def _organization_category(path):
    categories = {
        "Images": {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"},
        "Documents": {".doc", ".docx", ".md", ".pdf", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx"},
        "Audio": {".aac", ".flac", ".m4a", ".mp3", ".wav"},
        "Video": {".avi", ".mkv", ".mov", ".mp4", ".wmv"},
        "Archives": {".7z", ".gz", ".rar", ".tar", ".zip"},
    }
    extension = path.suffix.casefold()
    for category, extensions in categories.items():
        if extension in extensions:
            return category
    return "Other"


def _plan_download_organization():
    downloads = get_user_folders()["downloads"]
    if not downloads.is_dir():
        raise FileNotFoundError(f"Downloads folder was not found: {downloads}")

    moves = []
    for path in downloads.iterdir():
        if path.is_file():
            target_directory = downloads / _organization_category(path)
            moves.append((path, _unique_destination(target_directory / path.name)))
            if len(moves) > 100:
                raise ValueError("There are more than 100 files; organize a smaller batch first.")
    return moves


def _replace_or_append_file(path, mode, content):
    if not _is_within_user_folders(path) or not path.is_file():
        raise ValueError("That file is outside the folders Jarvis is allowed to manage.")
    if path.suffix.casefold() not in {".txt", ".md", ".csv", ".log", ".json"}:
        raise ValueError("For safety, Jarvis can edit only plain-text files.")
    if path.stat().st_size > 1_000_000:
        raise ValueError("That file is too large to edit safely (limit: 1 MB).")

    log_path = _record_action("edit_file", path, "confirmed")
    original = path.read_text(encoding="utf-8", errors="replace")
    backup = _unique_destination(path.with_name(f"{path.name}.jarvis-backup"))
    backup.write_text(original, encoding="utf-8")
    updated = content if mode == "replace" else original + content
    temporary_path = path.with_name(f"{path.name}.jarvis-tmp")
    try:
        temporary_path.write_text(updated, encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
    try:
        log_path = _record_action("edit_file", path, "completed")
    except OSError as exc:
        return (
            f"Updated {path} and saved a backup at {backup}, but could not record completion "
            f"in the action log: {exc}"
        )
    return f"Updated {path}. A backup was saved at {backup}. Action log: {log_path}."


def execute_confirmed_action(request):
    if request.action == "shutdown":
        log_path = _record_action("shutdown", "computer", "confirmed")
        response = shutdown_laptop()
        return f"{response} Action log: {log_path}."

    if request.action == "edit_file":
        return _replace_or_append_file(
            Path(request.payload["path"]),
            request.payload["mode"],
            request.payload["content"],
        )

    if request.action == "organize_downloads":
        moves = request.payload["moves"]
        move_summary = "; ".join(f"{source} -> {destination}" for source, destination in moves)
        log_path = _record_action("organize_downloads", move_summary, "confirmed")
        completed = []
        try:
            for source, destination in moves:
                destination.parent.mkdir(parents=True, exist_ok=True)
                final_destination = _unique_destination(destination)
                shutil.move(str(source), str(final_destination))
                completed.append((source, final_destination))
        except OSError as exc:
            moved = ", ".join(f"{source} -> {destination}" for source, destination in completed)
            log_warning = ""
            try:
                _record_action("organize_downloads", moved, "partial")
            except OSError as log_exc:
                logger.error("Could not record partial Downloads organization: %s", log_exc)
                log_warning = f" Partial action-log update failed: {log_exc}"
            return (
                f"Organization stopped after {len(completed)} file(s): {exc}. "
                f"Completed moves: {moved or 'none'}. Action log: {log_path}.{log_warning}"
            )
        try:
            log_path = _record_action("organize_downloads", move_summary, "completed")
        except OSError as exc:
            return f"Organized {len(completed)} file(s), but could not record completion in the action log: {exc}"
        return f"Organized {len(completed)} file(s) in Downloads. Action log: {log_path}."

    if request.action == "power":
        from automation import tools

        key = request.payload["action"]
        log_path = _record_action("power", key, "confirmed")
        return f"{tools.run_power_action(key)} Action log: {log_path}."

    raise ValueError(f"Unsupported confirmation action: {request.action}")


def parse_text_file_request(text_input):
    raw_command = text_input.strip()
    marker = re.search(
        r"\b(?:create|make|write|save)\s+(?:(?:a|me)\s+)?(?:(?:simple|plain|new)\s+)?(?:text\s*file|txt\s*file|file)\b",
        raw_command,
        re.IGNORECASE,
    )
    if not marker:
        return None

    remainder = raw_command[marker.end():].strip()
    explicit_content = re.search(
        r"\b(?:with\s+(?:the\s+)?(?:content|text)|containing)\s+(.+)$",
        remainder,
        re.IGNORECASE,
    )
    topic_match = re.search(r"\babout\s+(.+)$", remainder, re.IGNORECASE)
    name_match = re.search(
        r"\b(?:named|called)\s+(.+?)(?=\s+(?:with\s+(?:the\s+)?(?:content|text)|containing|on\s+(?:my\s+)?desktop)\b|$)",
        remainder,
        re.IGNORECASE,
    )

    topic = topic_match.group(1).strip(" .?!") if topic_match else ""
    file_name = name_match.group(1).strip().strip("\"'") if name_match else ""
    if not file_name and topic:
        safe_topic = re.sub(r"[^A-Za-z0-9 _-]", "", topic).strip().replace(" ", "_")
        file_name = f"{safe_topic[:60] or 'Jarvis'}_Essay.txt"
    elif not file_name:
        file_name = f"Jarvis_Note_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
    elif not file_name.lower().endswith(".txt"):
        file_name += ".txt"

    return {
        "filename": file_name,
        "content": explicit_content.group(1).strip() if explicit_content else "",
        "topic": topic,
    }


def create_desktop_text_file(file_name, content):

    clean_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(file_name).name).strip(" .")
    if not clean_name:
        raise ValueError("The requested file name is not valid.")
    if not clean_name.lower().endswith(".txt"):
        clean_name += ".txt"

    desktop = get_desktop_directory()
    desktop.mkdir(parents=True, exist_ok=True)
    file_path = desktop / clean_name
    suffix = 2
    while file_path.exists():
        file_path = desktop / f"{Path(clean_name).stem}_{suffix}.txt"
        suffix += 1

    file_path.write_text(content.strip() + "\n", encoding="utf-8")
    remember_last_file(file_path)
    return file_path


def open_last_created_file():
    global LAST_CREATED_FILE
    LAST_CREATED_FILE = last_written_file()
    if LAST_CREATED_FILE is None:
        return "I could not find a recently created text file to open."

    try:
        if hasattr(os, "startfile"):
            _open_notepad_with_ui(LAST_CREATED_FILE)
        else:
            subprocess.Popen(["xdg-open", str(LAST_CREATED_FILE)])
        return f"Opened your file: {LAST_CREATED_FILE}."
    except (ImportError, OSError, RuntimeError, TimeoutError) as exc:
        return f"I created the file, but could not open it: {exc}"


def open_notepad_and_write(text_content):
    """Creates a note file with content and opens it in Notepad."""
    notes_dir = notes_directory()
    notes_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = notes_dir / f"notepad_{timestamp}.txt"

    if text_content and text_content.strip():
        file_path.write_text(text_content.strip(), encoding="utf-8")
    else:
        file_path.write_text("", encoding="utf-8")

    try:
        subprocess.Popen(["notepad.exe", str(file_path)])
        return f"Opening Notepad and writing your note to {file_path}."
    except Exception as exc:
        return f"I could not open Notepad: {exc}"


def shutdown_laptop():
    """Initiates a full system shutdown."""
    try:
        subprocess.run(["shutdown", "/s", "/t", "0"], check=True)
        return "Shutting down the system, Sarthak. Your machine is powering down."
    except Exception as exc:
        return f"I could not shut down the laptop: {exc}"


def create_todo_task(task_text):
    """Adds a small task reminder to a local todo file on the desktop."""
    clean = (task_text or "").strip()
    if not clean:
        return "Tell me the task you want to save, Sarthak."

    tasks_dir = notes_directory()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_file = tasks_dir / "todo.txt"

    existing = task_file.read_text(encoding="utf-8") if task_file.exists() else ""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {clean}\n"
    task_file.write_text(existing + entry, encoding="utf-8")
    return f"Task saved to {task_file}."


def open_browser_search(query):
    """Opens a browser search for the given query."""
    safe_query = quote(query.strip())
    url = f"https://www.google.com/search?q={safe_query}"
    webbrowser.open(url)
    return f"Searching the web for {query.strip()} in your browser."


def set_system_volume(action):
    """Modulates Windows master sound channels directly via native virtual keyboard strokes."""
    try:
        if "mute" in action:
            subprocess.run(["powershell", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"], stdout=subprocess.DEVNULL)
            return "Toggling primary master audio channel mute matrix, Sarthak."
        elif "increase" in action:
            for _ in range(5):
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"], stdout=subprocess.DEVNULL)
            return "Amplifying system audio amplitude vectors, Sarthak."
        elif "decrease" in action:
            for _ in range(5):
                subprocess.run(["powershell", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"], stdout=subprocess.DEVNULL)
            return "Attenuating system audio amplitude vectors, Sarthak."
    except Exception:
        pass
    return "Audio matrix transformation command failed."


FILLER_PREFIX = re.compile(
    r"^(?:(?:hey|hello|hi|ok|okay|please|can you|could you|would you|will you|"
    r"jarvis|joy was|you know|um|uh|like|the)[\s,]+)+"
)
QUICK_WEBSITES = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google mail": "https://mail.google.com",
    "my email": "https://mail.google.com",
    "email": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "outlook": "https://outlook.live.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "my drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "my calendar": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
}
QUICK_APPS = {
    "chrome": "chrome", "google chrome": "chrome", "browser": "chrome",
    "edge": "edge", "microsoft edge": "edge",
    "vscode": "code", "vs code": "code", "visual studio code": "code", "code editor": "code",
    "notepad": "notepad", "notebook": "notepad", "note book": "notepad",
    "calculator": "calculator", "calc": "calculator",
    "paint": "paint",
    "file explorer": "explorer", "explorer": "explorer",
    "terminal": "terminal", "command prompt": "terminal", "cmd": "terminal",
}


DISPLAY_NAMES = {"youtube": "YouTube", "github": "GitHub", "gmail": "Gmail", "chatgpt": "ChatGPT",
                 "linkedin": "LinkedIn", "leetcode": "LeetCode", "whatsapp web": "WhatsApp Web"}


def display_name(name):
    return DISPLAY_NAMES.get(name, name.title())


def normalize_command(text_input):
    command = text_input.strip().lower()
    command = command.replace("sjut", "shut").replace("foen", "down").replace("fwen", "down")
    command = command.replace("note book", "notebook").replace("notepadbook", "notepad")
    command = re.sub(r"\s+", " ", command).strip(" .!?,")
    command = FILLER_PREFIX.sub("", command)
    command = re.sub(r"\s+please$", "", command).strip(" .!?,")
    return command


def trigger_action(text_input):
    """Handles quick, single-step commands instantly (no language model needed).

    Patterns here must match the WHOLE command, so that multi-step requests like
    "open notepad and write an essay about the sun" or questions that merely mention
    a keyword ("how does binary search work?") go to the agent instead.
    Returns a string, a ConfirmationRequest, "termination", or None.
    """
    raw_command = text_input.strip()
    command = normalize_command(raw_command)
    if not command or command == "the":
        return None

    from automation import tools

    if re.fullmatch(
        r"(?:(?:yes|yeah|yep|ok|okay|sure)\s+)?(?:go ahead and |now )?send(?: it| that| this| the (?:message|mail|email|reply|whatsapp))?(?: now)?",
        command,
    ):
        try:
            return tools.send_pending()
        except tools.ToolError as exc:
            return str(exc)
    if tools.has_pending_delete() and re.fullmatch(
        r"(?:yes|yeah|yep|ok|okay|sure)?,? ?(?:(?:go ahead and |please )?delete (?:it|that|them)(?: now)?|"
        r"confirm(?: (?:it|delete|deletion))?|(?:yes|yeah|yep),? (?:go ahead|do it))",
        command,
    ):
        try:
            return tools.confirm_delete()
        except tools.ToolError as exc:
            return str(exc)
    if re.fullmatch(r"(?:no,? )?(?:don't|do not) delete(?: it| that)?", command):
        return tools.clear_pending()

    if re.fullmatch(
        r"(?:no,? )?(?:don't|do not) send(?: it| that)?|cancel(?: it| that| the (?:message|mail|email|reply))?|"
        r"discard(?: it| that)?|never ?mind",
        command,
    ):
        return tools.clear_pending()

    if re.search(r"\bopen\s+(?:it|that file|the file|the text file)\b", command):
        return open_last_created_file()

    if any(k in command for k in ["turn off jarvis", "shutdown jarvis", "goodbye jarvis", "go offline", "sleep jarvis", "stop jarvis"]):
        return "termination"

    if re.fullmatch(r"(?:what(?:'s| is)? the )?(?:current )?time(?: is it)?(?: now| right now)?|what time is it(?: now| right now)?|tell me the time", command):
        return get_current_time_report()

    if re.fullmatch(r"(?:what(?:'s| is)? )?(?:today's|the|the current) date(?: today)?|what day is (?:it|today)|what is today", command):
        now = datetime.datetime.now()
        return f"Today's date is {now.strftime('%A, %B %d, %Y')}."

    if re.fullmatch(r"(?:open|show)(?: my| the)? (?:project(?: folder)?|workspace)", command):
        return open_project_folder()

    folder = re.fullmatch(r"(?:open|show|go to)(?: my| the)? (desktop|downloads|documents)(?: folder)?", command)
    if folder:
        return open_user_folder(folder.group(1))

    find_match = re.search(
        r"\b(?:find|locate|search for)\s+(?:the\s+)?file\s+(.+)$",
        raw_command,
        re.IGNORECASE,
    )
    if find_match:
        matches, truncated = find_user_files(find_match.group(1))
        if not matches:
            return f"I could not find a matching file in Desktop, Documents, or Downloads."
        result = "Found these files:\n" + "\n".join(str(path) for path in matches)
        if truncated:
            result += "\nResults may be incomplete because the search reached its safety limit."
        return result

    read_match = re.search(r"\bread\s+(?:the\s+)?file\s+(.+)$", raw_command, re.IGNORECASE)
    if read_match:
        try:
            return read_user_text_file(read_match.group(1))
        except (FileNotFoundError, OSError, ValueError) as exc:
            return f"I could not read that file: {exc}"

    replace_match = re.search(
        r"\breplace\s+(?:the\s+)?file\s+(.+?)\s+with\s+(.+)$",
        raw_command,
        re.IGNORECASE,
    )
    append_match = re.search(
        r"\bappend\s+to\s+(?:the\s+)?file\s+(.+?)\s*:\s*(.+)$",
        raw_command,
        re.IGNORECASE,
    )
    edit_match = replace_match or append_match
    if edit_match:
        mode = "replace" if replace_match else "append"
        file_query = edit_match.group(1).strip().strip("\"'")
        content = edit_match.group(2).strip().strip("\"'")
        try:
            path = _resolve_user_file(file_query)
            if path.stat().st_size > 1_000_000:
                raise ValueError("That file is too large to edit safely (limit: 1 MB).")
            preview = content[:200] + ("..." if len(content) > 200 else "")
            return ConfirmationRequest(
                action="edit_file",
                payload={"path": str(path), "mode": mode, "content": content},
                prompt=(
                    f"This will {mode} the text file {path}. Preview: {preview!r}. "
                    "A backup will be created. Type CONFIRM to proceed or CANCEL."
                ),
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            return f"I could not prepare that edit: {exc}"

    if re.search(r"\borganize\s+(?:my\s+)?downloads(?:\s+folder)?(?:\s+by\s+(?:file\s+)?type)?\b", command):
        try:
            moves = _plan_download_organization()
            if not moves:
                return "Your Downloads folder has no files to organize."
            preview = "\n".join(
                f"{source.name} -> {destination.parent.name}\\{destination.name}"
                for source, destination in moves[:10]
            )
            if len(moves) > 10:
                preview += f"\n...and {len(moves) - 10} more file(s)."
            return ConfirmationRequest(
                action="organize_downloads",
                payload={"moves": moves},
                prompt=(
                    f"I plan to move {len(moves)} file(s) into type-based folders:\n{preview}\n"
                    "Type CONFIRM to proceed or CANCEL."
                ),
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            return f"I could not prepare the Downloads organization: {exc}"

    file_request = parse_text_file_request(raw_command)
    if file_request:
        if not file_request["content"]:
            return "Tell me what you would like written in the text file, Sarthak."
        try:
            path = create_desktop_text_file(file_request["filename"], file_request["content"])
            return f"Created the file {path} for you, Sarthak."
        except (OSError, ValueError) as exc:
            return f"I could not create the text file: {exc}"

    if re.fullmatch(r"(?:shut ?down|turn off|power off|switch off)(?: (?:the|my))? (?:laptop|computer|pc|system)", command):
        return ConfirmationRequest(
            action="shutdown",
            payload={},
            prompt="This will shut down Windows immediately. Type CONFIRM to proceed or CANCEL.",
        )

    # --- Half a command ("open my", "play the"): ask instead of guessing ---
    filler = r"(?: (?:my|the|a|an|up|me|some))"
    half = re.fullmatch(r"(open|launch|close|search for|send|delete|create|make)" + filler + "*", command) or \
        re.fullmatch(r"(play|start)" + filler + "+", command)  # bare "play" still means play/pause
    if half:
        verb = half.group(1).split()[0]
        return f"{verb.capitalize()} what, {tools.user_name()}?"

    # --- "Open the X folder in VS Code / a terminal" (whole command only) ---
    in_app = re.fullmatch(
        r"(?:(?:i want you to|can you|please) )?open (?:up )?(?:my |the )?(.+?)(?: folder| directory)? "
        r"(?:in|through|with|using|via|on) (?:the )?(vs code|vscode|visual studio code|visual studio|code editor|"
        r"terminal|command prompt|powershell)(?: (?:can you|please|for me))?",
        command,
    )
    apps = r"(vs code|vscode|visual studio code|visual studio|code editor|terminal|command prompt|powershell)"
    app_first = re.fullmatch(  # "in vs code open the folder named html"
        r"(?:now )?(?:in|with|using|through) (?:the )?" + apps + r",? (?:(?:please|can you) )?open (?:up )?"
        r"(?:a |the |my )?(?:folder|directory)(?: (?:named|name|called))? (.+?)",
        command,
    )
    named = re.fullmatch(  # "open a folder named html in vs code"
        r"(?:(?:i want you to|can you|please) )?open (?:up )?(?:a |the |my )?(?:folder|directory) "
        r"(?:named|name|called) (.+?) (?:in|through|with|using|via|on) (?:the )?" + apps + r"(?: please| for me)?",
        command,
    )
    if named:
        target = (named.group(1), named.group(2))
    elif app_first:
        target = (app_first.group(2), app_first.group(1))
    elif in_app:
        target = (in_app.group(1), in_app.group(2))
    else:
        target = None
    if target:
        try:
            return tools.open_folder(target[0], app=target[1])
        except tools.ToolError as exc:
            return str(exc)

    # --- Quick "open X" for well-known apps and sites (whole command only) ---
    open_match = re.fullmatch(r"(?:open|launch|start|run|go to)(?: up)?(?: the| my)? (.+?)(?: app| website| site)?", command)
    if open_match:
        target = open_match.group(1).strip()
        if target in QUICK_APPS:
            return open_desktop_app(QUICK_APPS[target])
        if target in QUICK_WEBSITES:
            webbrowser.open(QUICK_WEBSITES[target])
            return f"Opening {display_name(target.replace('my ', ''))} for you, Sarthak."

        # Any other single "open X" goes straight to the app opener (no language model, so it's instant).
        if len(target.split()) <= 4 and not re.search(
                r"\b(?:and|then|with|to|about|in|on|for|from|through|using|via|it|this|that|tab|file|email|mail|message|chat|"
                r"video|song|last|latest|new)\b", target):
            if re.search(r" (?:folder|directory)$", target):
                try:
                    return tools.open_folder(target)
                except tools.ToolError as exc:
                    return str(exc)
            try:
                return tools.open_app(target)
            except tools.ToolError:
                pass
            try:
                return tools.open_folder(target)  # no app by that name: maybe it's a folder
            except tools.ToolError:
                return None  # let the agent work out what was meant

    # --- "Play X on YouTube" (whole command only) ---
    yt = re.fullmatch(r"(?:play|put on)(?: me)? (.+?) (?:on|from|in) youtube", command)
    if yt and not re.search(r"\b(?:and|then)\b", yt.group(1)):
        try:
            return tools.youtube_play(query=yt.group(1).strip())
        except tools.ToolError as exc:
            return str(exc)

    # --- "Create a folder called X (on/in Y)" (whole command only) ---
    new_folder = re.fullmatch(
        r"(?:create|make)(?: me)?(?: a)?(?: new)? folder (?:called|named|with the name) (.+?)"
        r"(?: (?:on|in|inside) (?:the |my )?(.+?))?",
        command,
    )
    if new_folder and not re.search(r"\b(?:and|then)\b", new_folder.group(1)):
        try:
            return tools.create_folder(new_folder.group(1), new_folder.group(2))
        except tools.ToolError as exc:
            return str(exc)

    # --- Recording (whole command only) ---
    if re.fullmatch(r"(?:stop|end|finish)(?: the)?(?: camera| video| screen)? recording(?: now)?", command):
        return tools.stop_recording()
    if re.fullmatch(r"(?:start )?(?:the )?record(?:ing)?(?: a)?(?: video)?(?: of me)? (?:from|with|on|using) (?:the |my )?(?:camera|webcam)|"
                    r"start (?:the )?(?:camera|webcam|video) recording", command):
        try:
            return tools.camera_record()
        except tools.ToolError as exc:
            return str(exc)

    # --- Volume (whole command only) ---
    by_amount = re.fullmatch(
        r"(increase|raise|turn up|decrease|lower|reduce|turn down)(?: the)? (?:volume|sound) by (\d{1,3})(?: ?%| percent)?",
        command,
    )
    if by_amount:
        direction = "up" if by_amount.group(1) in {"increase", "raise", "turn up"} else "down"
        try:
            return tools.set_volume(change=direction, amount=int(by_amount.group(2)))
        except tools.ToolError as exc:
            return str(exc)
    level = re.fullmatch(r"(?:set|change|put|turn)(?: the)? (?:volume|sound)(?: level)? (?:to|at) (\d{1,3})(?: ?%| percent)?", command)
    try:
        if level:
            return tools.set_volume(level=int(level.group(1)))
        if re.fullmatch(r"(?:mute|unmute)(?: (?:the|my))?(?: volume| sound| audio| laptop| computer| speakers)?|sound off", command):
            return tools.set_volume(change="mute")
        if re.fullmatch(r"(?:volume up|louder|(?:increase|raise|turn up)(?: the)? (?:volume|sound))", command):
            return tools.set_volume(change="up")
        if re.fullmatch(r"(?:volume down|quieter|(?:decrease|lower|reduce|turn down)(?: the)? (?:volume|sound|voice))", command):
            return tools.set_volume(change="down")
        if re.fullmatch(r"(?:play|pause|resume)(?: the)?(?: music| song| video)?", command):
            return tools.media_control("play_pause")
        if re.fullmatch(r"(?:next|skip)(?: the)?(?: song| track)?", command):
            return tools.media_control("next")
        if re.fullmatch(r"lock(?: the| my)? (?:screen|laptop|computer|pc)", command):
            return tools.lock_screen()
    except tools.ToolError as exc:
        return str(exc)

    search = re.match(r"^(?:search for|search|look up|google)\s+(.+)$", command)
    if search and not re.search(r"\b(?:private|incognito|inprivate|youtube)\b", command):
        query = search.group(1).strip()
        youtube = re.fullmatch(r"(.+?) on youtube", query)
        if youtube:
            return tools.youtube_search(youtube.group(1))
        return open_browser_search(query)

    note_match = re.match(r"^(?:create|make|save|write|take)\s+(?:a\s+)?note\b[:,]?\s*(.*)", raw_command.strip(), re.IGNORECASE)
    if note_match and not re.search(r"\bnotepad\b", raw_command, re.IGNORECASE):
        note_text = note_match.group(1).strip()
        if not note_text:
            return "Tell me what you want written in the note, Sarthak."
        return create_note_file(note_text)

    if any(phrase in command for phrase in ["read my latest note", "read the latest note", "what is my latest note", "read my last note"]):
        return read_latest_note()

    if any(phrase in command for phrase in ["read my tasks", "read my todo", "what are my tasks", "list my tasks", "show my tasks"]):
        return read_todo_tasks()

    task_match = re.match(r"^(?:add|create|remember)\s+(?:a\s+)?(?:task|todo|to-do)\b[:,]?\s*(.*)", command)
    if task_match:
        task_text = task_match.group(1).strip()
        if not task_text:
            return "Tell me the task you want me to remember, Sarthak."
        return create_todo_task(task_text)

    if command.startswith("remember this"):
        task_text = re.sub(r"^remember this\b[:,]?", "", command).strip()
        if not task_text:
            return "Tell me what to remember, Sarthak."
        return create_todo_task(task_text)

    if re.fullmatch(r"(?:start |enter |activate )?(?:project|coding) mode|start coding", command):
        print("🚀 [Jarvis Core]: Initializing software development environment...")
        try:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            subprocess.Popen(["code", project_root])
            webbrowser.open("https://github.com")
            return "Development matrix successfully deployed, Sarthak. Visual Studio Code initialized, repository workspace open."
        except Exception as e:
            return f"Workspace initialization deployment hit an anomaly: {str(e)}"

    if re.fullmatch(r"(?:run (?:a )?)?(?:security (?:sweep|scan)|safety check|scan (?:the )?(?:project )?files)", command):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        print(f"🛡️ [Jarvis Core]: Initializing SHA-256 security sweep over: {project_root}")
        return run_security_scan(project_root)

    if re.fullmatch(r"(?:give me (?:a )?)?(?:system status|status report|hardware report|system report)", command):
        print("📊 [Jarvis Core]: Fetching kernel hardware diagnostics...")
        return get_hardware_metrics()

    return None
