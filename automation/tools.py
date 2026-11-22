"""Laptop actions Jarvis can carry out on your behalf.

The agent (brain/agent.py) reads TOOLS, asks the language model to plan which tools
to call, and runs them through execute(). Every tool returns a short sentence that
Jarvis speaks, or an app.ConfirmationRequest for anything risky (shutdown, restart,
sign-out). Nothing here runs arbitrary shell commands, deletes files or sends email
on its own: email is always left as a draft for you to review and send.
"""
import ctypes
import datetime
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

from automation import app

IS_WINDOWS = os.name == "nt"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTACTS_FILE = PROJECT_ROOT / "contacts.json"
JARVIS_WINDOW_TITLE = "Jarvis | Desktop Assistant"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_GENERATED_CHARS = 20000
MAX_MAIL_URL_BODY = 1800

_notifier = None
_start_apps_cache = None


class ToolError(Exception):
    """A tool failed in a way the user should hear about."""


def user_name():
    return os.getenv("JARVIS_USER_NAME", "Sarthak").strip() or "Sarthak"


def set_notifier(callback):
    """Lets the UI receive messages from background tools such as reminders."""
    global _notifier
    _notifier = callback


# The last thing Jarvis prepared for you to send ("send it" sends exactly this).
_pending = {}
_pending_lock = threading.Lock()


def set_pending(kind, label, **data):
    with _pending_lock:
        _pending.clear()
        _pending.update({"kind": kind, "label": label, **data})


def get_pending():
    with _pending_lock:
        return dict(_pending)


def clear_pending():
    with _pending_lock:
        had = bool(_pending)
        _pending.clear()
    if cancel_delete():
        return "Okay, I won't delete it."
    return "Okay, I won't send it." if had else "Okay."


def set_pending_email_reply(message, body):
    from automation import mail

    set_pending(
        "email_reply", f"your reply to {message['from']}", to=message["reply_to"],
        subject=mail.reply_subject(message["subject"]), body=body,
        in_reply_to=message.get("message_id"), references=message.get("references"),
    )


def _names_match(said, pending):
    """True if the name the user said plausibly refers to the pending message's recipient."""
    import difflib

    said = re.sub(r"\s+", " ", str(said or "")).strip().casefold()
    target = pending.get("label", "").casefold()
    address = str(pending.get("to") or "").casefold()
    if not said or said in target or (address and said == address):
        return True
    contact, contact_email = find_contact(said, "email")
    if contact and contact_email and contact_email.casefold() == address:
        return True
    words = re.findall(r"[a-z]+", target)
    return any(difflib.SequenceMatcher(None, said, word).ratio() >= 0.8 for word in words) or (
        contact is not None and contact in target)


def send_pending(to=None):
    """Sends whatever Jarvis last prepared: a WhatsApp message, an email draft or an email reply.

    If the user named someone, it must be the person the message is for."""
    pending = get_pending()
    if not pending:
        raise ToolError("There's nothing waiting to be sent.")
    if to and not _names_match(to, pending):
        raise ToolError(f"The message that's ready is {pending['label']}, not to {to}, so I didn't send anything. "
                        "If that's who you meant, just say 'send it'.")
    kind = pending["kind"]
    if kind == "whatsapp":
        result = whatsapp_send()
    elif kind == "email_draft":
        result = email_send()
    elif kind == "email_reply":
        from automation import mail

        try:
            mail.send_email(pending["to"], pending["subject"], pending["body"],
                            pending.get("in_reply_to"), pending.get("references"))
        except mail.MailError as exc:
            raise ToolError(str(exc)) from exc
        result = f"Sent {pending['label']}."
    else:
        raise ToolError("I don't know how to send that.")
    clear_pending()
    return result


def _require_windows(feature):
    if not IS_WINDOWS:
        raise ToolError(f"{feature} only works on Windows.")


def _powershell(script, input_text=None, timeout=20, env=None):
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script],
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
        env={**os.environ, **(env or {})},
    )
    if result.returncode:
        raise ToolError((result.stderr or "PowerShell command failed.").strip()[:300])
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Keyboard (native key presses, no extra packages)
# ---------------------------------------------------------------------------
KEYEVENTF_EXTENDEDKEY = 0x1
KEYEVENTF_KEYUP = 0x2
MODIFIER_KEYS = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
NAMED_KEYS = {
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "capslock": 0x14,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplaypause": 0xB3,
}
NAMED_KEYS.update({f"f{number}": 0x6F + number for number in range(1, 13)})
EXTENDED_KEYS = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}
KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "windows": "win", "window": "win", "super": "win",
    "escape": "esc", "return": "enter", "del": "delete", "page up": "pageup",
    "page down": "pagedown", "print screen": "printscreen", "prtsc": "printscreen",
    "arrow up": "up", "arrow down": "down", "arrow left": "left", "arrow right": "right",
    "spacebar": "space",
}


def _key_code(name):
    name = KEY_ALIASES.get(name, name)
    if name in MODIFIER_KEYS:
        return MODIFIER_KEYS[name]
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ToolError(f"I don't know the key '{name}'.")


def _key_event(code, key_up=False):
    flags = KEYEVENTF_KEYUP if key_up else 0
    if code in EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    ctypes.windll.user32.keybd_event(code, 0, flags, 0)


def _tap(code, times=1, delay=0.02):
    _require_windows("Pressing keys")
    for _ in range(times):
        _key_event(code)
        _key_event(code, key_up=True)
        time.sleep(delay)


def parse_hotkey(keys):
    """'ctrl+shift+s' -> [0x11, 0x10, 0x53]. Raises ToolError for unknown keys."""
    text = re.sub(r"\s*\+\s*", "+", str(keys).strip().lower())
    for alias in sorted(KEY_ALIASES, key=len, reverse=True):
        if " " in alias:
            text = text.replace(alias, KEY_ALIASES[alias])
    names = [part.strip() for part in re.split(r"[+\s]+", text) if part.strip()]
    if not names or len(names) > 4:
        raise ToolError("Give me between one and four keys, like ctrl+s.")
    return [_key_code(name) for name in names]


def _press_combo(codes):
    _require_windows("Pressing keys")
    for code in codes:
        _key_event(code)
        time.sleep(0.02)
    for code in reversed(codes):
        _key_event(code, key_up=True)
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Windows and clipboard
# ---------------------------------------------------------------------------
def foreground_window_title():
    if not IS_WINDOWS:
        return ""
    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    return buffer.value


def _leave_jarvis_window():
    """If Jarvis's own window has focus (you typed the command), switch back to the
    window you were using before, so typing and shortcuts land there instead."""
    if JARVIS_WINDOW_TITLE in foreground_window_title():
        _press_combo([MODIFIER_KEYS["alt"], NAMED_KEYS["tab"]])
        time.sleep(0.6)


def _set_clipboard(text):
    _require_windows("The clipboard")
    _powershell(
        "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
        "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
        input_text=text,
    )


# ---------------------------------------------------------------------------
# Writing helpers
# ---------------------------------------------------------------------------
def _write(text, write_about, purpose):
    if text and str(text).strip():
        return str(text).strip()
    if not write_about or not str(write_about).strip():
        raise ToolError(f"Tell me what to write for the {purpose}.")
    from brain import llm  # imported here to avoid a circular import

    try:
        content = llm.generate_text(str(write_about).strip())
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc
    if not content:
        raise ToolError("The language model returned nothing to write.")
    return content[:MAX_GENERATED_CHARS]


def _safe_file_stem(text, fallback):
    stem = re.sub(r"[^A-Za-z0-9 _-]", "", str(text or "")).strip().replace(" ", "_")
    return stem[:60] or fallback


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
WEBSITES = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google mail": "https://mail.google.com",
    "email": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "outlook": "https://outlook.live.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "google drive": "https://drive.google.com",
    "drive": "https://drive.google.com",
    "google calendar": "https://calendar.google.com",
    "calendar": "https://calendar.google.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
    "whatsapp web": "https://web.whatsapp.com",
    "netflix": "https://www.netflix.com",
    "leetcode": "https://leetcode.com",
    "stack overflow": "https://stackoverflow.com",
    "google docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
}

SETTINGS_PAGES = {
    "settings": "ms-settings:",
    "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "bluetooth": "ms-settings:bluetooth",
    "display": "ms-settings:display",
    "brightness": "ms-settings:display",
    "sound": "ms-settings:sound",
    "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep",
    "windows update": "ms-settings:windowsupdate",
    "updates": "ms-settings:windowsupdate",
    "apps": "ms-settings:appsfeatures",
    "notifications": "ms-settings:notifications",
    "storage": "ms-settings:storagesense",
    "personalization": "ms-settings:personalization",
    "wallpaper": "ms-settings:personalization-background",
    "night light": "ms-settings:nightlight",
    "mouse": "ms-settings:mousetouchpad",
    "touchpad": "ms-settings:devices-touchpad",
    "printers": "ms-settings:printers",
    "date and time": "ms-settings:dateandtime",
    "language": "ms-settings:regionlanguage",
    "privacy": "ms-settings:privacy",
    "vpn": "ms-settings:network-vpn",
    "hotspot": "ms-settings:network-mobilehotspot",
    "airplane mode": "ms-settings:network-airplanemode",
}

PROCESS_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe",
    "microsoft edge": "msedge.exe", "firefox": "firefox.exe", "brave": "brave.exe",
    "notepad": "notepad.exe", "vs code": "Code.exe", "vscode": "Code.exe", "code": "Code.exe",
    "visual studio code": "Code.exe", "spotify": "Spotify.exe", "word": "WINWORD.EXE",
    "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE", "teams": "ms-teams.exe",
    "whatsapp": "WhatsApp.exe", "discord": "Discord.exe", "zoom": "Zoom.exe",
    "calculator": "CalculatorApp.exe", "paint": "mspaint.exe", "vlc": "vlc.exe",
    "telegram": "Telegram.exe", "slack": "slack.exe", "obs": "obs64.exe",
    "camera": "WindowsCamera.exe", "windows camera": "WindowsCamera.exe", "webcam": "WindowsCamera.exe",
    "photos": "Photos.exe", "settings": "SystemSettings.exe", "clock": "Time.exe", "alarms": "Time.exe",
    "media player": "Microsoft.Media.Player.exe", "microsoft store": "WinStore.App.exe", "store": "WinStore.App.exe",
    "task manager": "Taskmgr.exe", "snipping tool": "SnippingTool.exe",
}
# Apps with nothing to save, so Jarvis may force them closed if they ignore a normal close.
SAFE_TO_FORCE = {"windowscamera.exe", "photos.exe", "systemsettings.exe", "time.exe", "calculatorapp.exe",
                 "microsoft.media.player.exe", "winstore.app.exe", "snippingtool.exe"}
PROTECTED_PROCESSES = {
    "explorer.exe", "python.exe", "pythonw.exe", "py.exe", "svchost.exe", "winlogon.exe",
    "csrss.exe", "lsass.exe", "services.exe", "dwm.exe", "system", "smss.exe",
    "wininit.exe", "taskmgr.exe", "powershell.exe", "cmd.exe", "conhost.exe",
}


def _load_start_apps():
    global _start_apps_cache
    if _start_apps_cache is None:
        output = _powershell(
            "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
            timeout=30,
        )
        data = json.loads(output) if output else []
        _start_apps_cache = [data] if isinstance(data, dict) else data
    return _start_apps_cache


def _find_start_app(name):
    wanted = name.casefold()
    apps = [entry for entry in _load_start_apps() if entry.get("Name") and entry.get("AppID")]
    if len(wanted.replace(" ", "")) < 3 or wanted in {"my", "the", "app", "this", "that", "it"}:
        return None  # too short to match safely ("my" would open MyASUS)
    matchers = [lambda app_name: app_name == wanted, lambda app_name: app_name.startswith(wanted)]
    if len(wanted) >= 4:
        matchers.append(lambda app_name: wanted in app_name)
    for matcher in matchers:
        for entry in apps:
            if matcher(entry["Name"].casefold()):
                return entry
    return None


def _open_url(url):
    webbrowser.open(url)


def open_app(name):
    """Opens an installed app, a well-known website, a standard folder or a settings page."""
    target = re.sub(r"\s+", " ", str(name or "")).strip().strip(".!?").casefold()
    target = re.sub(r"^(?:the|my)\s+", "", target)
    target = re.sub(r"\s+(?:app|application|website|site|folder)$", "", target)
    if not target:
        raise ToolError("Tell me which app to open.")

    if target in {"desktop", "downloads", "documents"}:
        return app.open_user_folder(target)
    if target in {"project", "project folder"}:
        return app.open_project_folder()

    known = app.open_desktop_app(target)
    if known:
        return known

    if target in WEBSITES:
        _open_url(WEBSITES[target])
        return f"Opening {app.display_name(target)} in your browser."

    if target in SETTINGS_PAGES:
        return open_settings(target)

    if IS_WINDOWS:
        try:
            entry = _find_start_app(target)
        except (ToolError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ToolError(f"I couldn't search your installed apps: {exc}") from exc
        if entry:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{entry['AppID']}"])
            return f"Opening {entry['Name']}."

    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", target):
        return open_website(target)
    raise ToolError(f"I couldn't find an app called {name} on this laptop.")


def _running_processes(target, process):
    """Running processes for an app: the exact exe name, else any process whose name contains the app's name."""
    import psutil

    procs = list(psutil.process_iter(["name"]))
    exact = [p for p in procs if (p.info["name"] or "").casefold() == process.casefold()]
    if exact:
        return exact
    compact = re.sub(r"[^a-z0-9]", "", target)
    if len(compact) < 4:
        return []
    return [p for p in procs
            if compact in re.sub(r"[^a-z0-9]", "", (p.info["name"] or "").casefold().removesuffix(".exe"))
            and (p.info["name"] or "").casefold() not in PROTECTED_PROCESSES]


def close_app(name, force=False):
    """Closes an app gracefully, so it can still ask you to save unsaved work. force=True ends it at once."""
    target = re.sub(r"\s+", " ", str(name or "")).strip().casefold()
    target = re.sub(r"^(?:the|my)\s+", "", target)
    target = re.sub(r"\s+(?:app|application)$", "", target)
    process = PROCESS_NAMES.get(target)
    if not process:
        if not re.fullmatch(r"[a-z0-9 _.-]{1,40}", target):
            raise ToolError("That doesn't look like an app name.")
        process = target.replace(" ", "")
        if not process.endswith(".exe"):
            process += ".exe"
    if process.casefold() in PROTECTED_PROCESSES:
        raise ToolError(f"For safety I won't close {process}.")
    _require_windows("Closing apps")

    if target in {"camera", "webcam", "windows camera"}:
        from automation import camera

        if camera.is_recording():
            return stop_recording()

    running = _running_processes(target, process)
    if not running:
        return f"{name} doesn't seem to be running."
    names = sorted({p.info["name"] for p in running if p.info["name"]})
    if any(n.casefold() in PROTECTED_PROCESSES for n in names):
        raise ToolError(f"For safety I won't close {', '.join(names)}.")

    def taskkill(extra):
        return subprocess.run(["taskkill", *extra, *sum((["/IM", n] for n in names), [])],
                              capture_output=True, text=True, creationflags=NO_WINDOW).returncode == 0

    if force:
        return f"Closed {name}." if taskkill(["/F"]) else f"I couldn't close {name}."
    if taskkill([]):
        return f"Closing {name}."
    if all(n.casefold() in SAFE_TO_FORCE for n in names):
        return f"Closed {name}." if taskkill(["/F"]) else f"I couldn't close {name}."
    return (f"I asked {name} to close. If it has unsaved work it may be waiting for you to choose Save or "
            "Don't Save. Say 'force close' if it's stuck.")


# --- Recording -------------------------------------------------------------
_screen_recording = False


def camera_record():
    """Starts recording video from the webcam (closing the Camera app first, since it holds the webcam)."""
    from automation import camera

    if camera.is_recording():
        return "I'm already recording from your camera. Say 'stop recording' when you're done."
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/IM", "WindowsCamera.exe"], capture_output=True, creationflags=NO_WINDOW)
        time.sleep(0.5)
    try:
        path = camera.start()
    except camera.CameraError as exc:
        raise ToolError(str(exc)) from exc
    return (f"Recording from your camera now, video only without sound. Say 'stop recording' when you're done. "
            f"It's saving to {path.parent.name} in your Videos folder.")


def screen_record():
    """Starts or stops recording the active window with Xbox Game Bar (Win+Alt+R), sound included."""
    global _screen_recording
    _require_windows("Screen recording")
    _leave_jarvis_window()
    _press_combo([MODIFIER_KEYS["win"], MODIFIER_KEYS["alt"], ord("R")])
    _screen_recording = not _screen_recording
    if _screen_recording:
        return ("Started recording the window in front with Game Bar. Say 'stop recording' to finish. "
                "If nothing happens, turn on Game Bar in Settings, Gaming.")
    return "Stopped the screen recording. It's in Videos, Captures."


def stop_recording():
    """Stops whatever Jarvis is recording: the webcam, or the screen."""
    from automation import camera

    result = camera.stop()
    if result:
        path, seconds = result
        if IS_WINDOWS:
            os.startfile(str(path.parent))
        return f"Stopped recording. Saved {round(seconds)} seconds of video as {path.name} in Videos, Jarvis Recordings."
    if _screen_recording:
        return screen_record()
    return "Nothing is recording right now."


def write_in_notepad(text=None, write_about=None, title=None, read_aloud=False, folder=None):
    """Writes text (or generates it) into a new file and opens it in Notepad."""
    target = _resolve_folder(folder) if folder else None
    content = _write(text, write_about, "note")
    notes_dir = target or app.notes_directory()
    notes_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = _safe_file_stem(title or write_about, f"Jarvis_Note_{stamp}")
    path = notes_dir / f"{stem}.txt"
    suffix = 2
    while path.exists():
        path = notes_dir / f"{stem}_{suffix}.txt"
        suffix += 1
    path.write_text(content + "\n", encoding="utf-8")
    app.remember_last_file(path)
    if IS_WINDOWS:
        subprocess.Popen(["notepad.exe", str(path)])
    where = f"the {target.name} folder" if target else "your Jarvis Notes folder"
    message = f"I've written it in Notepad and saved it as {path.name} in {where}."
    return f"{message} Here it is: {content}" if read_aloud else message


def read_last_written():
    """Reads out the last file Jarvis wrote (essay, note, letter)."""
    path = app.last_written_file()
    if path is None:
        raise ToolError("I haven't written anything yet.")
    content = path.read_text(encoding="utf-8", errors="replace").strip()
    return f"Here's {path.stem.replace('_', ' ')}: {content[:6000]}"


def type_text(text=None, write_about=None):
    """Types text (or generated text) into whichever window you're using."""
    content = _write(text, write_about, "text to type")
    _require_windows("Typing")
    _leave_jarvis_window()
    window = foreground_window_title() or "the active window"
    _set_clipboard(content)
    time.sleep(0.2)
    _press_combo([MODIFIER_KEYS["ctrl"], ord("V")])
    return f"Typed {len(content)} characters into {window}."


def press_keys(keys):
    """Presses a keyboard shortcut such as ctrl+s, alt+tab or win+d."""
    codes = parse_hotkey(keys)
    _require_windows("Pressing keys")
    _leave_jarvis_window()
    _press_combo(codes)
    return f"Pressed {keys}."


def window_action(action):
    actions = {
        "minimize_all": ([MODIFIER_KEYS["win"], ord("D")], "Showing the desktop."),
        "show_desktop": ([MODIFIER_KEYS["win"], ord("D")], "Showing the desktop."),
        "switch_window": ([MODIFIER_KEYS["alt"], NAMED_KEYS["tab"]], "Switched windows."),
        "close_window": ([MODIFIER_KEYS["alt"], NAMED_KEYS["f4"]], "Closed the window."),
        "maximize": ([MODIFIER_KEYS["win"], NAMED_KEYS["up"]], "Maximized the window."),
        "minimize": ([MODIFIER_KEYS["win"], NAMED_KEYS["down"]], "Minimized the window."),
        "snap_left": ([MODIFIER_KEYS["win"], NAMED_KEYS["left"]], "Snapped the window left."),
        "snap_right": ([MODIFIER_KEYS["win"], NAMED_KEYS["right"]], "Snapped the window right."),
        "task_view": ([MODIFIER_KEYS["win"], NAMED_KEYS["tab"]], "Opened Task View."),
    }
    key = str(action or "").strip().lower().replace(" ", "_")
    if key not in actions:
        raise ToolError(f"Unknown window action. Use one of: {', '.join(actions)}.")
    _require_windows("Window control")
    _leave_jarvis_window()
    codes, message = actions[key]
    _press_combo(codes)
    return message


def set_volume(level=None, change=None, amount=10):
    """Sets the volume to an exact level, or turns it up/down/mutes it."""
    _require_windows("Volume control")
    if level is not None:
        level = max(0, min(100, int(level)))
        _tap(NAMED_KEYS["volumedown"], 50)  # Windows volume keys move 2% per press
        _tap(NAMED_KEYS["volumeup"], round(level / 2))
        return f"Volume set to {level} percent."
    change = str(change or "").strip().lower()
    steps = max(1, min(50, round(int(amount or 10) / 2)))
    if change in {"up", "increase", "louder"}:
        _tap(NAMED_KEYS["volumeup"], steps)
        return f"Turned the volume up by {steps * 2} percent."
    if change in {"down", "decrease", "lower", "quieter"}:
        _tap(NAMED_KEYS["volumedown"], steps)
        return f"Turned the volume down by {steps * 2} percent."
    if change in {"mute", "unmute", "toggle_mute"}:
        _tap(NAMED_KEYS["volumemute"])
        return "Toggled mute."
    raise ToolError("Tell me a volume level, or say up, down or mute.")


def set_brightness(level=None, change=None, amount=10):
    """Sets screen brightness on a laptop's built-in display."""
    _require_windows("Brightness control")
    try:
        current = int(_powershell(
            "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"
        ).split()[0])
    except (ToolError, ValueError, IndexError) as exc:
        raise ToolError("I couldn't read the brightness. This only works on a built-in laptop screen.") from exc
    if level is None:
        change = str(change or "").strip().lower()
        delta = int(amount or 10)
        if change in {"up", "increase", "brighter"}:
            level = current + delta
        elif change in {"down", "decrease", "dimmer", "lower"}:
            level = current - delta
        else:
            raise ToolError("Tell me a brightness level, or say up or down.")
    level = max(0, min(100, int(level)))
    _powershell(
        f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, {level})"
    )
    return f"Brightness set to {level} percent."


def media_control(action):
    keys = {
        "play": "mediaplaypause", "pause": "mediaplaypause", "play_pause": "mediaplaypause",
        "resume": "mediaplaypause", "next": "medianext", "skip": "medianext",
        "previous": "mediaprev", "back": "mediaprev", "stop": "mediastop",
    }
    key = keys.get(str(action or "").strip().lower().replace(" ", "_"))
    if not key:
        raise ToolError("Say play, pause, next, previous or stop.")
    _tap(NAMED_KEYS[key])
    return {"medianext": "Skipping to the next track.", "mediaprev": "Going back a track.",
            "mediastop": "Stopping playback."}.get(key, "Toggled play and pause.")


def open_website(url):
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.IGNORECASE) and not re.match(r"^[^/]+\.[a-z]{2,}:\d", url, re.IGNORECASE):
            raise ToolError("I only open http and https web addresses.")
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not re.fullmatch(
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?|localhost(?::\d+)?", parsed.netloc
    ):
        raise ToolError("That doesn't look like a valid web address.")
    _open_url(url)
    return f"Opening {parsed.netloc}."


def web_search(query):
    query = str(query or "").strip()
    if not query:
        raise ToolError("Tell me what to search for.")
    _open_url("https://www.google.com/search?q=" + quote(query))
    return f"Searching the web for {query}."


def youtube_search(query):
    query = str(query or "").strip()
    if not query:
        raise ToolError("Tell me what to look for on YouTube.")
    _open_url("https://www.youtube.com/results?search_query=" + quote(query))
    return f"Here are the YouTube results for {query}. Pick the one you want."


LATEST_WORDS = re.compile(r"\b(?:latest|newest|new|recent|last)\b", re.IGNORECASE)
VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


POPULAR_WORDS = re.compile(r"\b(?:most (?:viewed|watched|liked|like|popular|famous|views|likes)|top viewed|popular)\b",
                           re.IGNORECASE)


def find_youtube_video(query):
    """Returns the video id of the top YouTube result (newest first for 'latest ...'), or None."""
    import urllib.request

    url = "https://www.youtube.com/results?search_query=" + quote(POPULAR_WORDS.sub(" ", query).strip() or query)
    if LATEST_WORDS.search(query):
        url += "&sp=CAI%253D"  # YouTube's "sort by upload date"
    elif POPULAR_WORDS.search(query):
        url += "&sp=CAM%253D"  # YouTube's "sort by view count" (it has no sort by likes)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    page = ""
    with urllib.request.urlopen(request, timeout=6) as response:
        while len(page) < 2_000_000:
            chunk = response.read(65536)
            if not chunk:
                break
            page += chunk.decode("utf-8", errors="replace")
            match = VIDEO_ID.search(page)
            if match:
                return match.group(1)  # no need to download the rest of the page
    return None


LIKED_SONGS_WORDS = {"liked songs", "liked", "my liked songs", "favourites", "favorites", "my favourites",
                     "my favorites", "liked song playlist", "liked songs playlist", "my songs"}


def spotify_play(what=None):
    """Opens Spotify on Liked Songs or a search, and presses play."""
    _require_windows("Spotify")
    target = re.sub(r"\s+", " ", str(what or "")).strip().casefold()
    target = re.sub(r"^(?:my|the)\s+|\s+(?:on|from|in) spotify$|\s+playlist$", "", target).strip()
    if not target:
        uri, label = "spotify:", "Spotify"
    elif target in LIKED_SONGS_WORDS or "liked" in target:
        uri, label = "spotify:collection:tracks", "your Liked Songs"
    else:
        uri, label = "spotify:search:" + quote(target), f"Spotify results for {target}"
    try:
        os.startfile(uri)
    except OSError as exc:
        raise ToolError("Spotify doesn't seem to be installed.") from exc
    if not target:
        time.sleep(3)
        _tap(NAMED_KEYS["mediaplaypause"])
        return "Opening Spotify and resuming your music."
    return (f"I've opened {label}. Spotify doesn't let other programs pick a song to start, so click the green "
            "play button, or say 'play' to resume what you were last listening to.")


def youtube_play(query):
    """Opens and starts the top YouTube video for the query."""
    query = str(query or "").strip()
    if not query:
        raise ToolError("Tell me what to play.")
    try:
        video_id = find_youtube_video(query)
    except Exception:
        video_id = None
    if not video_id:
        return youtube_search(query)
    _open_url(f"https://www.youtube.com/watch?v={video_id}")
    return f"Playing {query} on YouTube."


def open_settings(page):
    key = str(page or "settings").strip().lower()
    uri = SETTINGS_PAGES.get(key)
    if not uri:
        raise ToolError(f"I don't know that settings page. Try one of: {', '.join(sorted(SETTINGS_PAGES))}.")
    _require_windows("Settings")
    os.startfile(uri)
    return "Opening Settings." if key == "settings" else f"Opening {key} settings."


def _load_contacts():
    """contacts.json entries can be "name": "email" or "name": {"email": "...", "phone": "..."}."""
    if not CONTACTS_FILE.is_file():
        return {}
    try:
        data = json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    contacts = {}
    for name, value in data.items():
        if isinstance(value, str):
            value = {"email": value} if "@" in value else {"phone": value}
        if isinstance(value, dict):
            contacts[str(name).strip().casefold()] = {
                "email": str(value.get("email") or "").strip(),
                "phone": re.sub(r"[^\d]", "", str(value.get("phone") or "")),
            }
    return contacts


def find_contact(name, field):
    """Looks a person up by name (forgiving small mishearings). Returns (matched_name, value) or (None, "")."""
    import difflib

    wanted = re.sub(r"\s+", " ", str(name or "")).strip().casefold()
    if not wanted:
        return None, ""
    contacts = {key: entry[field] for key, entry in _load_contacts().items() if entry.get(field)}
    if wanted in contacts:
        return wanted, contacts[wanted]
    first_names = {key.split()[0]: key for key in contacts}
    if wanted in first_names:
        key = first_names[wanted]
        return key, contacts[key]
    close = difflib.get_close_matches(wanted, list(contacts), n=1, cutoff=0.75)
    if close:
        return close[0], contacts[close[0]]
    return None, ""


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def draft_email(to=None, subject=None, body=None, about=None):
    """Opens a pre-filled email draft. It never sends: you review it and press Send."""
    recipient_name = str(to or "").strip()
    address = ""
    if EMAIL_PATTERN.match(recipient_name):
        address = recipient_name
    elif recipient_name:
        _, address = find_contact(recipient_name, "email")

    subject = str(subject or "").strip()
    if not (body and str(body).strip()):
        if not about:
            raise ToolError("Tell me what the email should say.")
        greeting_name = recipient_name if recipient_name and not address == recipient_name else ""
        instruction = (
            f"Write an email{' to ' + greeting_name if greeting_name else ''} about: {about}. "
            f"Sign it from {user_name()}. Keep it clear and appropriately polite. "
            "Put the subject on the first line as 'Subject: ...', then a blank line, then the body."
        )
        generated = _write(None, instruction, "email")
        match = re.match(r"^\s*Subject:\s*(.+?)\s*\n+(.*)$", generated, re.DOTALL | re.IGNORECASE)
        if match:
            subject = subject or match.group(1).strip()
            body = match.group(2).strip()
        else:
            body = generated
    body = str(body).strip()

    copied_full_body = False
    url_body = body
    if len(body) > MAX_MAIL_URL_BODY:
        url_body = body[:MAX_MAIL_URL_BODY] + "\n[...]"
        try:
            _set_clipboard(body)
            copied_full_body = True
        except ToolError:
            pass

    provider = os.getenv("JARVIS_MAIL_PROVIDER", "gmail").strip().lower()
    if provider == "outlook":
        url = (
            "https://outlook.live.com/mail/0/deeplink/compose?to=" + quote(address)
            + "&subject=" + quote(subject) + "&body=" + quote(url_body)
        )
    elif provider == "default":
        url = f"mailto:{quote(address)}?subject={quote(subject)}&body={quote(url_body)}"
    else:
        url = (
            "https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(address)
            + "&su=" + quote(subject) + "&body=" + quote(url_body)
        )
    _open_url(url)

    set_pending("email_draft", f"the email to {recipient_name or address or 'your recipient'}")
    message = "Your email draft is open. Review it, then say 'send it' or press Send."
    if recipient_name and not address:
        message += (
            f" I don't have an address for {recipient_name}, so add it in the To field, "
            "or save it in contacts.json for next time."
        )
    if copied_full_body:
        message += " The full text was too long for the link, so I copied it to your clipboard; paste it over the body."
    return message


def add_contact(name, phone=None, email=None):
    """Saves or updates a person in contacts.json."""
    name = re.sub(r"\s+", " ", str(name or "")).strip().casefold()
    if not name or not (phone or email):
        raise ToolError("Tell me the person's name and their number or email.")
    entry = {}
    if phone:
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 10:  # a local number: add the country code
            digits = os.getenv("JARVIS_DEFAULT_COUNTRY_CODE", "91").strip() + digits
        if not 11 <= len(digits) <= 15:
            raise ToolError(f"{phone} doesn't look like a full phone number.")
        entry["phone"] = digits
    if email:
        email = str(email).strip().replace(" at ", "@").replace(" dot ", ".").replace(" ", "")
        if not EMAIL_PATTERN.match(email):
            raise ToolError(f"{email} doesn't look like an email address.")
        entry["email"] = email
    try:
        data = json.loads(CONTACTS_FILE.read_text(encoding="utf-8")) if CONTACTS_FILE.is_file() else {}
    except ValueError as exc:
        raise ToolError("contacts.json has a formatting mistake, so I didn't change it.") from exc
    key = next((existing_key for existing_key in data if str(existing_key).strip().casefold() == name), name)
    existing = data.get(key)
    merged = {"email": existing} if isinstance(existing, str) and "@" in existing else (
        dict(existing) if isinstance(existing, dict) else {})
    merged.update(entry)
    data[key] = merged
    data.pop("example person", None)
    CONTACTS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    saved = " and ".join(f"{key} {value}" for key, value in entry.items())
    return f"Saved {name.title()}'s {saved}."


BROWSER_WINDOW_WORDS = ("chrome", "edge", "firefox", "brave", "opera", "youtube")


def close_tab():
    """Closes the current browser tab (e.g. a YouTube video)."""
    _require_windows("Closing tabs")
    _leave_jarvis_window()
    title = foreground_window_title()
    if not any(word in title.casefold() for word in BROWSER_WINDOW_WORDS):
        raise ToolError(f"The window in front is {title or 'not a browser'}, so I didn't close anything.")
    _press_combo([MODIFIER_KEYS["ctrl"], ord("W")])
    return "Closed the tab."


def email_send():
    """Sends the email draft that is open in front (Ctrl+Enter in Gmail, Outlook and Mail)."""
    _require_windows("Sending email")
    _leave_jarvis_window()
    for _ in range(20):
        title = foreground_window_title().casefold()
        if any(word in title for word in ("gmail", "compose", "outlook", "mail", "message")):
            break
        time.sleep(0.3)
    else:
        raise ToolError("The email draft isn't the active window, so I didn't send anything.")
    _press_combo([MODIFIER_KEYS["ctrl"], NAMED_KEYS["enter"]])
    return "Email sent."


def _sender_filter(from_name):
    if not from_name:
        return None
    if EMAIL_PATTERN.match(str(from_name).strip()):
        return str(from_name).strip()
    _, address = find_contact(from_name, "email")
    return address or str(from_name).strip()


def check_email(unread_only=None, count=5, days=None, from_name=None, read_full=False):
    """Summarises recent emails, or reads one person's latest email in full. Read-only."""
    from automation import mail

    sender = _sender_filter(from_name)
    if unread_only is None:
        unread_only = days is None and not sender  # "mail from Adi" / "from yesterday" means read ones too
    if sender and days is None:
        days = 30
    try:
        messages = mail.fetch_messages(unread_only=bool(unread_only), limit=max(1, min(int(count or 5), 10)),
                                       days=days, from_filter=sender)
    except mail.MailError as exc:
        raise ToolError(str(exc)) from exc
    if not messages:
        if sender:
            return f"I couldn't find any recent email from {from_name}."
        if days is not None:
            period = "today" if days == 0 else "since yesterday" if days == 1 else f"in the last {days} days"
            return f"You have no {'unread ' if unread_only else ''}emails {period}."
        return "You have no unread emails." if unread_only else "You have no emails from the last week."
    if read_full or (sender and len(messages) == 1):
        latest = messages[0]
        return (f"Email from {latest['from']}, received {latest['date']}, subject: {latest['subject']}. "
                f"It says: {latest['body'][:1200]}")
    return mail.summarize(messages, user_name())


def reply_to_email(sender, instructions=None):
    """Drafts a reply to the latest email from someone and reads it out. 'send it' sends it."""
    from automation import mail

    address = _sender_filter(sender)
    if not address:
        raise ToolError("Tell me whose email to reply to.")
    try:
        messages = mail.fetch_messages(unread_only=False, limit=1, days=30, from_filter=address)
        if not messages:
            raise ToolError(f"I couldn't find a recent email from {sender}.")
        body = mail.draft_reply(messages[0], user_name(), instructions)
    except mail.MailError as exc:
        raise ToolError(str(exc)) from exc
    set_pending_email_reply(messages[0], body)
    return (f"Here's my reply to {messages[0]['from']} about {messages[0]['subject']}: {body} "
            "Say 'send it' to send it, or tell me what to change.")


def set_availability(status):
    """away / available / auto. While away, important personal emails get a polite automatic reply."""
    from automation import presence

    key = str(status or "").strip().lower()
    if key in {"away", "busy", "out", "unavailable", "not available"}:
        presence.set_away(True)
        return "Got it, you're away. I'll tell people who email you that you'll get back to them."
    if key in {"available", "back", "here", "online"}:
        presence.set_away(False)
        return "Welcome back. I'll tell you about important emails and draft replies for you."
    if key in {"auto", "automatic"}:
        presence.set_away(None)
        return "Okay, I'll decide from your keyboard and mouse activity whether you're away."
    raise ToolError("Say away, available or automatic.")


def parse_event_time(start, now=None):
    """'2026-11-19T15:00', '15:00' or '3 pm' -> datetime (today, or tomorrow if that time has passed)."""
    now = now or datetime.datetime.now()
    text = str(start or "").strip()
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "")).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", text.lower())
    if not match:
        raise ToolError(f"I couldn't understand the time {text!r}.")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "")
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 7:
        hour += 12  # "a meeting at 3" almost always means 3 PM
    if hour > 23 or minute > 59:
        raise ToolError(f"I couldn't understand the time {text!r}.")
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + datetime.timedelta(days=1)


def schedule_event(title, start, duration_minutes=60, details=None, remind_minutes_before=10):
    """Opens Google Calendar with the event filled in (you press Save) and sets a reminder."""
    when = parse_event_time(start)
    end = when + datetime.timedelta(minutes=max(5, int(duration_minutes or 60)))
    utc = datetime.timezone.utc
    stamp = lambda moment: moment.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    url = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE&text=" + quote(str(title))
        + f"&dates={stamp(when)}/{stamp(end)}"
        + ("&details=" + quote(str(details)) if details else "")
    )
    _open_url(url)
    lead = int(remind_minutes_before or 0)
    from automation import schedule

    schedule.save_local_event(str(title), when, end, lead)
    spoken_time = when.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")
    message = f"I've opened Google Calendar with {title} on {spoken_time}. Click Save to add it."
    minutes_until = (when - datetime.datetime.now()).total_seconds() / 60 - lead
    if lead > 0 and 0 < minutes_until <= 24 * 60:
        set_reminder(minutes_until, f"{title} starts in {lead} minutes.")
        message += f" I'll also remind you {lead} minutes before, as long as I'm running."
    return message


def get_schedule(day="today", part="all"):
    """Reads out what's on the calendar for a day (or part of a day)."""
    from automation import schedule

    try:
        start, end = schedule.day_window(day, part)
        events, connected = schedule.events_between(start, end)
    except schedule.ScheduleError as exc:
        raise ToolError(str(exc)) from exc
    day_text = str(day or "today").strip().lower()
    label = day_text if day_text in {"today", "tomorrow", "yesterday"} else "on " + start.strftime("%A %d %B")
    if label == "today" and str(part or "all").lower() in {"afternoon", "morning", "evening", "night"}:
        label = "this"
    return schedule.describe(events, label, str(part or "all").lower(), connected)


def restore_reminders():
    """Re-arms reminders for upcoming events Jarvis scheduled (they'd otherwise be lost on restart)."""
    from automation import schedule

    now = datetime.datetime.now()
    restored = 0
    for event in schedule.load_local_events():
        if event["remind"] <= 0:
            continue
        minutes_until = (event["start"] - now).total_seconds() / 60 - event["remind"]
        if 0 < minutes_until <= 24 * 60:
            set_reminder(minutes_until, f"{event['title']} starts in {event['remind']} minutes.")
            restored += 1
    return restored


def whatsapp_message(to, text=None, about=None):
    """Opens a WhatsApp chat with the message typed in. Sending is a separate step (whatsapp_send)."""
    message = _write(text, f"Write a short, casual WhatsApp message to {to} about: {about}. "
                     "Plain text, no quotes, no sign-off." if about else None, "WhatsApp message")
    matched, phone = find_contact(to, "phone")
    if not phone:
        # No number saved: open WhatsApp's own contact picker with the message typed in.
        link = f"whatsapp://send?text={quote(message)}"
        try:
            _require_windows("WhatsApp")
            os.startfile(link)
        except (OSError, ToolError):
            _open_url(f"https://web.whatsapp.com/send?text={quote(message)}")
        set_pending("whatsapp", f"the WhatsApp message to {to}")
        return (f"WhatsApp is open with your message: {message} Pick {to} from the list and press Send. "
                f"To skip this step next time, say: save {to}'s number, followed by the number.")
    link = f"whatsapp://send?phone={phone}&text={quote(message)}"
    try:
        _require_windows("WhatsApp")
        os.startfile(link)
    except (OSError, ToolError):
        _open_url(f"https://web.whatsapp.com/send?phone={phone}&text={quote(message)}")
    set_pending("whatsapp", f"the WhatsApp message to {matched.title()}")
    return f"Your message to {matched.title()} is ready in WhatsApp: {message} Say 'send it' to send."


def whatsapp_send():
    """Presses Enter in WhatsApp to send the message that's waiting in the chat box."""
    _require_windows("Sending WhatsApp messages")
    _leave_jarvis_window()
    for _ in range(20):
        if "whatsapp" in foreground_window_title().casefold():
            break
        time.sleep(0.3)
    else:
        raise ToolError("WhatsApp isn't the active window, so I didn't send anything.")
    _press_combo([NAMED_KEYS["enter"]])
    return "Sent."


BROWSER_PATHS = {
    "chrome": ("--incognito", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                               r"%LocalAppData%\Google\Chrome\Application\chrome.exe"]),
    "edge": ("--inprivate", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]),
}


def private_browse(query=None, url=None):
    """Opens a private (incognito) browser window, optionally with a search or web address."""
    _require_windows("Private browsing")
    if url:
        target = str(url).strip()
        if not re.match(r"^https?://", target, re.IGNORECASE):
            target = "https://" + target
    elif query:
        target = "https://www.google.com/search?q=" + quote(str(query).strip())
    else:
        target = "about:blank"
    for name, (flag, candidates) in BROWSER_PATHS.items():
        for candidate in candidates:
            path = os.path.expandvars(candidate)
            if os.path.isfile(path):
                subprocess.Popen([path, flag, target])
                return f"Opened a private {name.title()} window."
    raise ToolError("I couldn't find Chrome or Edge for private browsing.")


def take_screenshot():
    _require_windows("Screenshots")
    _press_combo([MODIFIER_KEYS["win"], NAMED_KEYS["printscreen"]])
    return "Screenshot saved to your Pictures, Screenshots folder."


def lock_screen():
    _require_windows("Locking the screen")
    ctypes.windll.user32.LockWorkStation()
    from automation import security

    security.lock()
    return "Locking your laptop."


POWER_ACTIONS = {
    "shutdown": (["shutdown", "/s", "/t", "0"], "shut down Windows"),
    "restart": (["shutdown", "/r", "/t", "0"], "restart Windows"),
    "sign_out": (["shutdown", "/l"], "sign you out of Windows"),
}


def power_action(action):
    """Shutdown, restart or sign out. Always needs you to type CONFIRM first."""
    key = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = {"shut_down": "shutdown", "reboot": "restart", "log_out": "sign_out", "logout": "sign_out",
           "sign_off": "sign_out"}.get(key, key)
    if key not in POWER_ACTIONS:
        raise ToolError("Say shutdown, restart or sign out.")
    return app.ConfirmationRequest(
        action="power",
        payload={"action": key},
        prompt=f"This will {POWER_ACTIONS[key][1]} immediately. Type CONFIRM to proceed or CANCEL.",
    )


def run_power_action(key):
    command, _ = POWER_ACTIONS[key]
    subprocess.run(command, check=True)
    return {"shutdown": "Shutting down now.", "restart": "Restarting now.",
            "sign_out": "Signing you out."}[key]


def set_reminder(minutes, message):
    minutes = float(minutes)
    if not 0 < minutes <= 24 * 60:
        raise ToolError("I can set reminders from 1 minute up to 24 hours ahead.")
    text = str(message or "").strip() or "This is your reminder."

    def fire():
        note = f"Reminder: {text}"
        if _notifier:
            _notifier(note)
        else:
            print(f"[Reminder] {note}")

    timer = threading.Timer(minutes * 60, fire)
    timer.daemon = True
    timer.start()
    due = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    return f"Okay, I'll remind you at {due.strftime('%I:%M %p').lstrip('0')}. Keep me running until then."


def wait(seconds):
    seconds = max(0.0, min(10.0, float(seconds)))
    time.sleep(seconds)
    return ""


FOLDER_SEARCH_SECONDS = 4.0
FOLDER_SEARCH_MAX_DIRS = 80000
_SKIP_FOLDERS = {
    "appdata", "node_modules", "__pycache__", "site-packages", "windows", "program files",
    "program files (x86)", "programdata", "system volume information", "venv", "env", "$recycle.bin",
}


def _standard_folders():
    desktop = app.get_desktop_directory()
    return {
        "desktop": desktop,
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "screenshots": Path.home() / "Pictures" / "Screenshots",
        "music": Path.home() / "Music",
        "videos": Path.home() / "Videos",
        "jarvis notes": desktop / "Jarvis Notes",
        "notes": desktop / "Jarvis Notes",
        "project": PROJECT_ROOT,
        "jarvis": PROJECT_ROOT,
        "home": Path.home(),
    }


def _folder_words(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).split()


def _folder_search_roots():
    """Where to look for a folder by name: the user's own folders first, then other drives."""
    home = Path.home()
    roots = [app.get_desktop_directory(), home / "Desktop", home / "Documents", home / "Downloads",
             home / "Pictures", home / "Videos", home / "Music"]
    one_drive = os.getenv("OneDrive")
    if one_drive:
        roots.append(Path(one_drive))
    roots.append(home)
    if IS_WINDOWS:
        for letter in "DEFGHIJ":
            drive = Path(f"{letter}:\\")
            if drive.exists():
                roots.append(drive)
    seen, unique = set(), []
    for root in roots:
        key = str(root).casefold()
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _score_folder(name_words, wanted_words):
    """3 = same name, 2 = name starts with the request, 1 = contains every word, 0 = no match."""
    if not name_words:
        return 0
    if name_words == wanted_words:
        return 3
    if name_words[:len(wanted_words)] == wanted_words:
        return 2
    joined = " ".join(name_words)
    if all(word in joined for word in wanted_words) or "".join(wanted_words) == "".join(name_words):
        return 1
    return 0


def find_folders(query, limit=5, roots=None):
    """Searches the user's folders (and other drives) for directories matching a spoken name."""
    return _find_entries(query, want_files=False, limit=limit, roots=roots)


def find_files(query, limit=5, roots=None):
    """Searches for files by spoken name ("sun essay", "report dot pdf")."""
    return _find_entries(query, want_files=True, limit=limit, roots=roots)


def _spoken_file_name(query):
    text = re.sub(r"\s+dot\s+", ".", str(query or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+(?:file|document)$", "", text, flags=re.IGNORECASE)


def _find_entries(query, want_files=False, limit=5, roots=None):
    skip = {"the", "my", "folder", "directory"} if not want_files else {"the", "my", "file"}
    query = _spoken_file_name(query) if want_files else query
    wanted = [w for w in _folder_words(query) if w not in skip]
    if not wanted:
        return []
    has_extension = want_files and bool(re.search(r"\.[a-z0-9]{1,5}$", str(query).strip(), re.IGNORECASE))
    found = []  # (score, depth, path)
    seen = set()
    queue = [(root, 0) for root in (roots or _folder_search_roots())]
    deadline = time.monotonic() + FOLDER_SEARCH_SECONDS
    inspected = 0
    while queue and inspected < FOLDER_SEARCH_MAX_DIRS and time.monotonic() < deadline:
        folder, depth = queue.pop(0)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        if depth > 0 and {e.name.casefold() for e in entries} & {"pyvenv.cfg", "python.exe"}:
            continue  # inside a Python install or virtual environment (its "Lib", "html"... aren't yours)
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            lowered = entry.name.casefold()
            if lowered.startswith((".", "$")) or (is_dir and lowered in _SKIP_FOLDERS):
                continue
            inspected += 1
            key = os.path.normcase(entry.path)
            if key in seen:
                continue
            seen.add(key)
            if is_dir != want_files:
                name = entry.name if (has_extension or not want_files) else os.path.splitext(entry.name)[0]
                score = _score_folder(_folder_words(name), wanted)
                if score:
                    found.append((score, depth, Path(entry.path)))
            if is_dir and depth < 6:
                queue.append((Path(entry.path), depth + 1))
        if any(score == 3 for score, _, _ in found) and depth >= 3:
            break  # an exact match near the top is good enough
    found.sort(key=lambda item: (-item[0], item[1], len(str(item[2]))))
    return [path for _, _, path in found[:limit]]


# The folder Jarvis last opened or created, so "save it in that folder" works.
_current_folder = None
_THAT_FOLDER = re.compile(r"^(?:(?:in|inside|into|to) )?(?:that|this|the same|the|it|current|same)(?: folder| one| directory)?$")


def _set_current_folder(folder):
    global _current_folder
    _current_folder = Path(folder)


def current_folder():
    return _current_folder if _current_folder is not None and _current_folder.is_dir() else None


def _resolve_folder(spoken, default=None):
    """A folder from what the user said: "that folder", "desktop", "capstone", or the default."""
    text = " ".join(_folder_words(spoken or ""))
    if not text:
        if default is not None:
            return default
        raise ToolError("Which folder?")
    if _THAT_FOLDER.match(text):
        folder = current_folder()
        if folder is None:
            raise ToolError("Which folder do you mean? Tell me its name.")
        return folder
    return _folder_location(text)


def _show_folder(folder):
    if IS_WINDOWS:
        os.startfile(str(folder))
    else:
        subprocess.Popen(["xdg-open", str(folder)])


FOLDER_APPS = {
    "vs code": "vscode", "vscode": "vscode", "visual studio code": "vscode", "visual studio": "vscode",
    "code": "vscode", "code editor": "vscode",
    "terminal": "terminal", "command prompt": "terminal", "cmd": "terminal", "powershell": "terminal",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
}


def _find_vscode():
    import shutil

    for base in (os.getenv("LOCALAPPDATA"), os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)")):
        if base:
            exe = Path(base) / ("Programs" if base == os.getenv("LOCALAPPDATA") else "") / "Microsoft VS Code" / "Code.exe"
            if exe.is_file():
                return [str(exe)]
    code = shutil.which("code")
    return ["cmd", "/c", "code"] if code else None


def _open_folder_in(folder, app_name):
    """Shows a folder in File Explorer, VS Code or a terminal. Returns the app's display name."""
    kind = FOLDER_APPS.get(" ".join(_folder_words(app_name or "")), "explorer" if not app_name else None)
    if kind is None:
        raise ToolError(f"I can open folders in File Explorer, VS Code or a terminal, not {app_name}.")
    if kind == "explorer":
        _show_folder(folder)
        return "File Explorer"
    _require_windows("Opening folders in apps")
    if kind == "vscode":
        command = _find_vscode()
        if not command:
            raise ToolError("I couldn't find VS Code on this laptop.")
        subprocess.Popen([*command, str(folder)], creationflags=NO_WINDOW if command[0] == "cmd" else 0)
        return "VS Code"
    subprocess.Popen(["powershell", "-NoExit"], cwd=str(folder), creationflags=subprocess.CREATE_NEW_CONSOLE)
    return "a terminal"


def open_folder(name, app=None):
    """Opens a folder by name (standard ones or any folder found by searching), in Explorer, VS Code or a terminal."""
    key = " ".join(_folder_words(name))
    key = re.sub(r"^(?:the |my )+", "", key)
    key = re.sub(r" (?:folder|directory)$", "", key).strip()
    if not key:
        raise ToolError("Tell me which folder to open.")
    standard = _standard_folders()
    others = []
    if _THAT_FOLDER.match(key) or key in {"that", "this", "it", "same"}:
        folder = current_folder()
        if folder is None:
            raise ToolError("Which folder do you mean? Tell me its name.")
    elif key in standard:
        folder = standard[key]
        if not folder.is_dir():
            raise ToolError(f"{folder} doesn't exist.")
    else:
        matches = find_folders(key)
        if not matches:
            raise ToolError(f"I couldn't find a folder called {key} on this laptop.")
        folder = matches[0]
        others = [m for m in matches[1:] if _folder_words(m.name) == _folder_words(folder.name)]
    shown_in = _open_folder_in(folder, app)
    _set_current_folder(folder)
    ending = "." if shown_in == "File Explorer" else f" in {shown_in}."
    if key in standard:
        return f"Opening your {key} folder" + ending
    where = "Desktop" if folder.parent == app_module_desktop() else (folder.parent.name or str(folder.parent))
    preposition = "in" if shown_in == "File Explorer" else "from"
    reply = f"Opening the {folder.name} folder {preposition} {where}" + ending
    if others:
        reply += f" There's another folder with that name in {others[0].parent.name}."
    return reply


def review_code(question=None, folder=None, file=None):
    """Reads the code in a folder (the one just opened, unless named) and explains or reviews it."""
    from automation import code_review
    from brain import llm

    target = _resolve_folder(folder) if folder else current_folder()
    if target is None:
        raise ToolError("Which folder's code should I look at? Open it first, or tell me its name.")
    if target.resolve() in _protected_paths():
        raise ToolError(f"Your whole {target.name} folder is too big to read. Tell me the project folder's name.")
    budget = 100_000 if llm.get_cloud_provider_candidates() else 20_000  # the offline model reads less at once

    def ask_ai(instruction):
        try:
            return llm.generate_text(instruction, max_tokens=3000)
        except RuntimeError as exc:
            raise ToolError(f"I couldn't reach the AI to read the code: {exc}") from exc

    try:
        spoken, details, used = code_review.review(target, question, file, ask_ai, budget)
    except code_review.CodeReviewError as exc:
        raise ToolError(str(exc)) from exc
    report = code_review.save_report(target, question, details, used, app.notes_directory())
    app.remember_last_file(report)
    if IS_WINDOWS:
        command = _find_vscode()
        if command:
            subprocess.Popen([*command, "-r", str(report)], creationflags=NO_WINDOW if command[0] == "cmd" else 0)
            where = "VS Code"
        else:
            os.startfile(str(report))
            where = "a new window"
    else:
        where = "your Jarvis Notes folder"
    files = f"{len(used)} file{'s' if len(used) != 1 else ''}"
    return f"{spoken} I read {files} and opened the full review in {where}."


def app_module_desktop():
    return app.get_desktop_directory()


_BAD_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _folder_location(location):
    """Turns a spoken place ("desktop", "documents", "my capstone folder") into a folder path."""
    key = " ".join(_folder_words(location or "desktop"))
    key = re.sub(r"^(?:the |my |on |in |inside )+", "", key)
    key = re.sub(r" (?:folder|directory)$", "", key).strip() or "desktop"
    standard = _standard_folders()
    if key in standard:
        return standard[key]
    matches = find_folders(key)
    if not matches:
        raise ToolError(f"I couldn't find a folder called {key} to put it in.")
    return matches[0]


def create_folder(name, location=None):
    """Creates a new folder (on the Desktop unless told where) and opens it."""
    clean = _BAD_FOLDER_CHARS.sub("", str(name or "")).strip().strip(".")
    clean = re.sub(r"\s+", " ", clean)[:100]
    if not clean:
        raise ToolError("What should I name the folder?")
    if clean.casefold() in _RESERVED_NAMES:
        raise ToolError(f"Windows doesn't allow a folder called {clean}. Pick another name.")
    parent = _resolve_folder(location, default=_standard_folders()["desktop"])
    folder = parent / clean
    try:  # Windows names ignore case: "capstone" is the same folder as "Capstone"
        folder = next((child for child in parent.iterdir()
                       if child.is_dir() and child.name.casefold() == clean.casefold()), folder)
    except OSError:
        pass
    where = "Desktop" if parent == app.get_desktop_directory() else parent.name or str(parent)
    if folder.is_dir():
        _show_folder(folder)
        _set_current_folder(folder)
        return f"A folder called {folder.name} already exists in {where}, so I opened it."
    try:
        folder.mkdir(parents=False)
    except OSError as exc:
        raise ToolError(f"I couldn't create the folder: {exc}") from exc
    _show_folder(folder)
    _set_current_folder(folder)
    return f"Created the {clean} folder in {where}."


def _unique_path(path):
    candidate, number = path, 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def _where(path):
    parent = path.parent
    return "Desktop" if parent == app.get_desktop_directory() else (parent.name or str(parent))


_LAST_WRITTEN = re.compile(r"^(?:it|that|this|the (?:essay|note|file|letter|text|document)|what you (?:just )?wrote|"
                           r"(?:the )?last (?:file|essay|note)|that (?:essay|note|file))$")


def _find_one(name, kind="any", inside=None):
    """Finds exactly one file or folder by spoken name. Raises if none or several equally good ones."""
    spoken = " ".join(_folder_words(name))
    if kind != "folder" and _LAST_WRITTEN.match(spoken):
        path = app.last_written_file()
        if path is None:
            raise ToolError("I haven't written anything yet, so tell me the file's name.")
        return path
    roots = [inside] if inside is not None else None
    results = []
    if kind in {"any", "folder"}:
        results += find_folders(name, limit=3, roots=roots)
    if kind in {"any", "file"}:
        results += find_files(name, limit=3, roots=roots)
    if not results:
        label = {"folder": "folder", "file": "file"}.get(kind, "file or folder")
        raise ToolError(f"I couldn't find a {label} called {name}.")
    best = results[0]
    same = [r for r in results[1:] if _folder_words(os.path.splitext(r.name)[0]) == _folder_words(os.path.splitext(best.name)[0])]
    if same and kind != "any":
        raise ToolError(f"I found {best.name} in {_where(best)} and also in {_where(same[0])}. "
                        f"Tell me which one, for example '{name} in {_where(best)}'.")
    return best


def save_to_folder(name, folder=None, copy=False):
    """Moves (or copies) a file or folder into another folder."""
    import shutil

    target = _resolve_folder(folder)
    source = _find_one(name)
    if source.resolve() == target.resolve() or target.resolve().is_relative_to(source.resolve()):
        raise ToolError("I can't put a folder inside itself.")
    _check_safe_to_change(source)
    destination = _unique_path(target / source.name)
    try:
        if copy:
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        else:
            shutil.move(str(source), str(destination))
    except OSError as exc:
        raise ToolError(f"I couldn't {'copy' if copy else 'move'} it: {exc}") from exc
    if not source.exists() and app.LAST_CREATED_FILE is not None and Path(app.LAST_CREATED_FILE) == source:
        app.remember_last_file(destination)
    _set_current_folder(target)
    return f"{'Copied' if copy else 'Moved'} {source.name} into the {target.name or target} folder."


def _protected_paths():
    home = Path.home()
    protected = {Path(p).resolve() for p in _standard_folders().values()}
    protected |= {home.resolve(), Path(home.anchor).resolve(), app.get_desktop_directory().resolve()}
    one_drive = os.getenv("OneDrive")
    if one_drive:
        protected.add(Path(one_drive).resolve())
    return protected


def _check_safe_to_change(path):
    """Refuses system folders, the user's main folders and Jarvis's own files."""
    resolved = path.resolve()
    if resolved in _protected_paths() or resolved == Path(resolved.anchor):
        raise ToolError(f"For safety I won't delete or move your {path.name or path} folder itself.")
    project = PROJECT_ROOT.resolve()
    if resolved == project or project.is_relative_to(resolved) or resolved.is_relative_to(project):
        raise ToolError("For safety I won't delete or move Jarvis's own files.")
    lowered = {part.casefold() for part in resolved.parts}
    if lowered & {"appdata", "windows", "program files", "program files (x86)", "programdata"}:
        raise ToolError("That's a system location, so I won't touch it.")


# A delete waiting for the user to say "yes, delete it".
_pending_delete = {}
DELETE_CONFIRM_SECONDS = 120


def _describe_for_delete(path):
    if path.is_dir():
        try:
            count = sum(1 for _ in path.rglob("*") if _.is_file())
        except OSError:
            count = 0
        inside = "it's empty" if count == 0 else f"with {count} file{'s' if count != 1 else ''} inside"
        return f"the {path.name} folder in {_where(path)}, {inside}"
    return f"{path.name} in {_where(path)}"


def delete_item(name, kind="any", folder=None):
    """Prepares to move a file or folder to the Recycle Bin; the user must confirm by voice."""
    inside = _resolve_folder(folder) if folder else None
    if kind == "folder" and _THAT_FOLDER.match(" ".join(_folder_words(name))):
        path = current_folder()
        if path is None:
            raise ToolError("Which folder should I delete? Tell me its name.")
    else:
        path = _find_one(name, kind if kind in {"file", "folder"} else "any", inside)
    _check_safe_to_change(path)
    _pending_delete.clear()
    _pending_delete.update({"path": path, "at": time.monotonic()})
    return (f"Should I delete {_describe_for_delete(path)}? It goes to the Recycle Bin, so you can restore it. "
            "Say 'yes, delete it' to confirm or 'cancel'.")


def has_pending_delete():
    return bool(_pending_delete) and time.monotonic() - _pending_delete["at"] <= DELETE_CONFIRM_SECONDS


def _send_to_recycle_bin(path):
    _require_windows("Deleting")
    kind = "DeleteDirectory" if path.is_dir() else "DeleteFile"
    _powershell(
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::{kind}($env:JARVIS_DELETE_PATH, 'OnlyErrorDialogs', 'SendToRecycleBin')",
        env={"JARVIS_DELETE_PATH": str(path)}, timeout=60,
    )


def confirm_delete():
    """Deletes what delete_item prepared, after the user said yes."""
    if not has_pending_delete():
        _pending_delete.clear()
        raise ToolError("There's nothing waiting to be deleted. Tell me what to delete first.")
    path = _pending_delete.pop("path")
    _pending_delete.clear()
    if not path.exists():
        raise ToolError(f"{path.name} isn't there anymore.")
    _check_safe_to_change(path)
    _send_to_recycle_bin(path)
    global _current_folder
    if _current_folder is not None and (Path(_current_folder) == path or Path(_current_folder).is_relative_to(path)):
        _current_folder = None
    return f"Deleted {path.name}. It's in the Recycle Bin if you need it back."


def cancel_delete():
    had = bool(_pending_delete)
    _pending_delete.clear()
    return had


def find_file(name):
    matches, truncated = app.find_user_files(str(name or ""))
    if not matches:
        return "I couldn't find a matching file in Desktop, Documents or Downloads."
    names = ", ".join(path.name for path in matches[:5])
    extra = " There may be more." if truncated or len(matches) > 5 else ""
    return f"I found {len(matches)} file{'s' if len(matches) != 1 else ''}: {names}.{extra}"


def open_file(name):
    try:
        path = app._resolve_user_file(str(name or ""))
    except (FileNotFoundError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    _require_windows("Opening files")
    os.startfile(str(path))
    return f"Opening {path.name}."


def read_text_file(name):
    try:
        text = app.read_user_text_file(str(name or ""))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    header, _, content = text.partition("\n")  # "Contents of C:\\...\\file.txt:" is not nice to hear
    file_name = re.split(r"[\\\\/]", header[len("Contents of "):].rstrip(":"))[-1]
    stem = os.path.splitext(file_name)[0].replace("_", " ")
    return f"Here's {stem}: {content.strip()}"


def organize_downloads():
    moves = app._plan_download_organization()
    if not moves:
        return "Your Downloads folder has no files to organize."
    return app.ConfirmationRequest(
        action="organize_downloads",
        payload={"moves": moves},
        prompt=f"I plan to move {len(moves)} file(s) in Downloads into type-based folders. Type CONFIRM to proceed or CANCEL.",
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    function: object
    description: str
    params: dict = field(default_factory=dict)  # name -> (type, required, description)


def _p(kind, required, description):
    return (kind, required, description)


TOOLS = {
    "open_app": Tool(open_app, "Open an installed app (e.g. Spotify, Word, WhatsApp), a well-known website (YouTube, Gmail, GitHub) or a settings page.",
                     {"name": _p("str", True, "app or site name")}),
    "close_app": Tool(close_app, "Close an app gracefully (it can still ask to save).",
                      {"name": _p("str", True, "app name"),
                       "force": _p("bool", False, "true only if the user says force close, kill or terminate")}),
    "write_in_notepad": Tool(write_in_notepad, "Write something into a new Notepad file and open it. Use for essays, notes, letters, lists.",
                             {"text": _p("str", False, "exact text, if the user dictated it"),
                              "write_about": _p("str", False, "full instruction for what to write, e.g. 'a 300-word essay about the sun'"),
                              "title": _p("str", False, "short file title"),
                              "read_aloud": _p("bool", False, "true if the user also asked to read it out"),
                              "folder": _p("str", False, "folder to save it in, e.g. 'capstone' or 'that folder'; "
                                           "leave out for the Jarvis Notes folder")}),
    "read_last_written": Tool(read_last_written, "Read out the last thing Jarvis wrote (the essay/note in Notepad)."),
    "spotify_play": Tool(spotify_play, "Open Spotify on the user's Liked Songs, a playlist or a search, or resume Spotify.",
                         {"what": _p("str", False, "'liked songs', a song/artist/playlist name, or empty to resume")}),
    "type_text": Tool(type_text, "Type text into the window the user is currently using (e.g. a chat box or document).",
                      {"text": _p("str", False, "exact text to type"),
                       "write_about": _p("str", False, "instruction for text to generate and type")}),
    "press_keys": Tool(press_keys, "Press a keyboard shortcut in the current window, e.g. ctrl+s, ctrl+t, alt+tab, enter.",
                       {"keys": _p("str", True, "keys joined with +")}),
    "window_action": Tool(window_action, "Control windows: minimize_all, switch_window, close_window, maximize, minimize, snap_left, snap_right, task_view.",
                          {"action": _p("str", True, "one of the listed actions")}),
    "set_volume": Tool(set_volume, "Set the volume to a level, or change it.",
                       {"level": _p("int", False, "0-100"), "change": _p("str", False, "up, down or mute"),
                        "amount": _p("int", False, "percent to change by, default 10")}),
    "set_brightness": Tool(set_brightness, "Set screen brightness to a level, or change it.",
                           {"level": _p("int", False, "0-100"), "change": _p("str", False, "up or down"),
                            "amount": _p("int", False, "percent to change by, default 10")}),
    "media_control": Tool(media_control, "Control music/video playback.",
                          {"action": _p("str", True, "play, pause, next, previous or stop")}),
    "open_website": Tool(open_website, "Open a specific web address.", {"url": _p("str", True, "URL or domain")}),
    "web_search": Tool(web_search, "Search Google in the browser.", {"query": _p("str", True, "search terms")}),
    "youtube_play": Tool(youtube_play, "Open and start playing the top YouTube video for a query, e.g. 'mrbeast latest video'. "
                         "Include 'latest' to get the newest upload, or 'most viewed' for the most popular one.",
                         {"query": _p("str", True, "what to play")}),
    "youtube_search": Tool(youtube_search, "Show YouTube search results without playing anything.", {"query": _p("str", True, "what to find")}),
    "open_settings": Tool(open_settings, "Open a Windows settings page: " + ", ".join(sorted(SETTINGS_PAGES)) + ".",
                          {"page": _p("str", True, "settings page name")}),
    "draft_email": Tool(draft_email, "Open an email draft for the user to review and send. Never sends by itself.",
                        {"to": _p("str", False, "email address, or the person's name exactly as the user said it"),
                         "subject": _p("str", False, "subject line"),
                         "body": _p("str", False, "exact body, only if the user dictated it"),
                         "about": _p("str", False, "what the email should say, if Jarvis should write it")}),
    "add_contact": Tool(add_contact, "Save a person's phone number and/or email for WhatsApp and email.",
                        {"name": _p("str", True, "the person's name"), "phone": _p("str", False, "phone number"),
                         "email": _p("str", False, "email address")}),
    "close_tab": Tool(close_tab, "Close the current browser tab, e.g. 'close the video' or 'close this tab'."),
    "check_email": Tool(check_email, "Read out who emailed the user and what about, or read one person's email in full (read-only).",
                        {"from_name": _p("str", False, "only mail from this person (name or address)"),
                         "read_full": _p("bool", False, "true to read the latest matching email word for word"),
                         "unread_only": _p("bool", False, "true for unread only; leave out when the user names a day or person"),
                         "count": _p("int", False, "how many, default 5, max 10"),
                         "days": _p("int", False, "how far back: 0 = today, 1 = since yesterday, 7 = this week")}),
    "reply_to_email": Tool(reply_to_email, "Draft a reply to the latest email from someone and read it out; the user says 'send it' to send.",
                           {"sender": _p("str", True, "who sent the email"),
                            "instructions": _p("str", False, "what the reply should say, if the user said")}),
    "send_pending": Tool(send_pending, "Send the message, draft or reply Jarvis just prepared. Only when the user says to send it.",
                         {"to": _p("str", False, "the person the user named, if they named anyone")}),
    "set_availability": Tool(set_availability, "Mark the user away (auto-replies to important emails), available, or automatic.",
                             {"status": _p("str", True, "away, available or auto")}),
    "schedule_event": Tool(schedule_event, "Put a meeting/event in Google Calendar (opens it pre-filled for the user to Save) and set a reminder.",
                           {"title": _p("str", True, "event name"),
                            "start": _p("str", True, "start as YYYY-MM-DDTHH:MM in the user's local time"),
                            "duration_minutes": _p("int", False, "default 60"),
                            "details": _p("str", False, "notes, people, link"),
                            "remind_minutes_before": _p("int", False, "default 10, 0 for none")}),
    "get_schedule": Tool(get_schedule, "Tell the user what's on their calendar.",
                         {"day": _p("str", False, "today (default), tomorrow, or YYYY-MM-DD"),
                          "part": _p("str", False, "all (default), morning, afternoon, evening or night")}),
    "whatsapp_message": Tool(whatsapp_message, "Open a WhatsApp chat with a message typed in, ready to send. Use one call per person.",
                             {"to": _p("str", True, "the person's name exactly as said"),
                              "text": _p("str", False, "exact message, if the user dictated it"),
                              "about": _p("str", False, "what the message should say, if Jarvis should write it")}),
    "private_browse": Tool(private_browse, "Open a private/incognito browser window, with a search or a web address.",
                           {"query": _p("str", False, "what to search for"), "url": _p("str", False, "web address")}),
    "take_screenshot": Tool(take_screenshot, "Take a screenshot of the whole screen."),
    "camera_record": Tool(camera_record, "Start recording a video from the webcam (video only). Use for 'record a "
                          "video of me', 'start recording from my camera', or 'start recording' while the Camera app "
                          "is open."),
    "screen_record": Tool(screen_record, "Start recording the screen (the window in front) with sound, via Game Bar."),
    "stop_recording": Tool(stop_recording, "Stop the current camera or screen recording."),
    "lock_screen": Tool(lock_screen, "Lock the laptop."),
    "power_action": Tool(power_action, "Shut down, restart or sign out (the user must type CONFIRM).",
                         {"action": _p("str", True, "shutdown, restart or sign_out")}),
    "set_reminder": Tool(set_reminder, "Remind the user after some minutes.",
                         {"minutes": _p("number", True, "minutes from now"), "message": _p("str", True, "what to remind")}),
    "wait": Tool(wait, "Pause briefly, e.g. to let an app finish opening before typing.",
                 {"seconds": _p("number", True, "1-10")}),
    "open_folder": Tool(open_folder, "Open ANY folder on the laptop by its name (searches Desktop, Documents, Downloads, "
                        "OneDrive and other drives), e.g. \"solo leveling\" or \"manga project\"; also downloads, "
                        "desktop, documents, pictures, music, videos, notes, project, or \"that folder\". "
                        "Can open it in VS Code or a terminal instead of File Explorer.",
                        {"name": _p("str", True, "the folder's name exactly as the user said it, or 'that folder'"),
                         "app": _p("str", False, "vs code or terminal, if the user asked to open it in one")}),
    "create_folder": Tool(create_folder, "Create a new folder and open it. On the Desktop unless the user says where "
                          "(documents, downloads, or inside another folder by name).",
                          {"name": _p("str", True, "the new folder's name"),
                           "location": _p("str", False, "where to create it, e.g. desktop, documents, or a folder name")}),
    "save_to_folder": Tool(save_to_folder, "Move (or copy) a file or folder into another folder. Use name \"it\" for "
                           "the last thing Jarvis wrote.",
                           {"name": _p("str", True, "the file or folder to move, e.g. 'sun essay', 'report.pdf', 'it'"),
                            "folder": _p("str", True, "destination folder name, or 'that folder' for the one just "
                                         "opened or created"),
                            "copy": _p("bool", False, "true to copy instead of move")}),
    "delete_item": Tool(delete_item, "Delete a file or folder (to the Recycle Bin). This only ASKS the user to confirm; "
                        "it deletes nothing by itself.",
                        {"name": _p("str", True, "file or folder name, or 'that folder'"),
                         "kind": _p("str", False, "file, folder or any"),
                         "folder": _p("str", False, "folder it's in, if the user said")}),
    "confirm_delete": Tool(confirm_delete, "Actually delete what delete_item asked about. Only when the user clearly says "
                           "yes to deleting right after Jarvis asked."),
    "review_code": Tool(review_code, "Read the code in a folder and explain it, review it, find bugs or answer a "
                        "question about it. Uses the folder Jarvis just opened unless the user names one. Read-only.",
                        {"question": _p("str", False, "what the user wants to know, e.g. 'find bugs' or 'what does "
                                        "app.js do'; leave out for a general review"),
                         "folder": _p("str", False, "folder name if the user said one"),
                         "file": _p("str", False, "a specific file name if the user named one, e.g. 'index.html'")}),
    "find_file": Tool(find_file, "Find files by name in Desktop, Documents and Downloads.", {"name": _p("str", True, "file name or part of it")}),
    "open_file": Tool(open_file, "Open a file from Desktop, Documents or Downloads with its default app.", {"name": _p("str", True, "file name")}),
    "read_text_file": Tool(read_text_file, "Read a .txt/.md/.csv/.log/.json file from Desktop, Documents or Downloads.", {"name": _p("str", True, "file name")}),
    "organize_downloads": Tool(organize_downloads, "Sort the Downloads folder into subfolders by file type (asks for confirmation)."),
    "create_note": Tool(lambda text: app.create_note_file(text), "Save a quick note.", {"text": _p("str", True, "note text")}),
    "add_task": Tool(lambda text: app.create_todo_task(text), "Add an item to the to-do list.", {"text": _p("str", True, "task")}),
    "read_tasks": Tool(lambda: app.read_todo_tasks(), "Read the to-do list."),
    "read_latest_note": Tool(lambda: app.read_latest_note(), "Read the most recent note."),
    "get_time": Tool(lambda: app.get_current_time_report(), "Tell the current time and date."),
    "system_status": Tool(lambda: app.get_hardware_metrics(), "Report CPU, RAM and battery."),
}


def describe_tools():
    lines = []
    for name, tool in TOOLS.items():
        if tool.params:
            args = ", ".join(
                f"{arg} ({kind}{', required' if required else ''}: {hint})"
                for arg, (kind, required, hint) in tool.params.items()
            )
        else:
            args = "no args"
        lines.append(f"- {name}: {tool.description} Args: {args}")
    return "\n".join(lines)


def _coerce(value, kind):
    if kind == "str":
        return str(value)
    if kind == "int":
        return int(round(float(value)))
    if kind == "number":
        return float(value)
    if kind == "bool":
        return value if isinstance(value, bool) else str(value).strip().lower() in {"true", "yes", "1"}
    return value


def execute(name, args=None):
    """Runs one tool by name with validated arguments.

    Returns a sentence for the user, or an app.ConfirmationRequest. Raises ToolError
    with a user-facing message when the tool can't be used or fails.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"I don't have a tool called {name}.")
    raw_args = args if isinstance(args, dict) else {}
    clean = {}
    for arg, (kind, required, _) in tool.params.items():
        value = raw_args.get(arg)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ToolError(f"{name} needs {arg}.")
            continue
        try:
            clean[arg] = _coerce(value, kind)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{arg} should be a {kind}, not {value!r}.") from exc
    try:
        return tool.function(**clean)
    except ToolError:
        raise
    except Exception as exc:  # report, don't crash the assistant
        raise ToolError(f"{name.replace('_', ' ')} failed: {exc}") from exc
