import os
import re
import sys
import ctypes
from ctypes import wintypes

print("[Startup Matrix] Phase 1: Allocating local subfolder paths...")
PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.append(PROJECT_DIR)

# Load API keys and settings from .env (previously this file was never read).
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
except ImportError:
    print("[Startup] python-dotenv is not installed, so .env is ignored. Run: pip install python-dotenv")

print("[Startup Matrix] Phase 2: Binding package modules into memory...")
from automation import app
from brain import llm
from mouth import speaker
from brain import agent
from automation import security

def _install_crash_logger():
    """If Jarvis crashes, print why and keep a copy in jarvis_crash.log (instead of closing silently)."""
    import threading
    import traceback

    log_path = os.path.join(PROJECT_DIR, "jarvis_crash.log")

    def report(kind, exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(f"\n[Crash] {kind}:\n{text}")
        try:
            import datetime

            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {kind}\n{text}\n")
        except OSError:
            pass

    def main_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        report("Jarvis stopped because of an error", exc_type, exc, tb)

    sys.excepthook = main_hook
    threading.excepthook = lambda args: report(
        f"Error in background task {getattr(args.thread, 'name', '')}", args.exc_type, args.exc_value, args.exc_traceback)


_install_crash_logger()

RUNNING_HEADLESS = False
_INSTANCE_MUTEX = None
USER_TITLE = os.getenv("JARVIS_USER_NAME", "Sarthak").strip() or "Sarthak"
AGENT_ENABLED = os.getenv("JARVIS_AGENT", "on").strip().lower() not in {"off", "0", "false", "no"}
_PENDING_ACTION = None


def acquire_single_instance():
    """Allows only one Jarvis process per Windows user session."""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    mutex_handle = kernel32.CreateMutexW(None, False, "Local\\JarvisAssistantSingleton")
    if not mutex_handle:
        print("[Startup] Could not establish the single-instance lock; refusing to start a duplicate.")
        return False
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(mutex_handle)
        print("[Startup] Jarvis is already running. This duplicate instance will exit.")
        return False

    _INSTANCE_MUTEX = mutex_handle
    return True


def personalize_response(text):
    return str(text).replace("Sarthak", USER_TITLE)

if RUNNING_HEADLESS:
    LOG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "jarvis_mainframe.log"))
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout

LOCK_PHRASES = re.compile(r"^(?:jarvis,? )?lock (?:yourself|jarvis|up|it)(?: now)?$|^(?:jarvis,? )?lock$")
TERMINATION_WORDS = {"terminate", "exit", "quit", "close jarvis", "exit jarvis", "bye jarvis"}


def _locked_reply(text_input):
    """While locked, the only thing Jarvis does is check the password (nothing reaches the AI or the laptop)."""
    if text_input == "SLEEP_SIGNAL_CONFIRMED":
        return "", False
    if text_input == "WAKE_SIGNAL_CONFIRMED":
        return "I'm locked. What's the password?", False
    if text_input.lower().strip(" .!") in TERMINATION_WORDS:
        return "Goodbye.", True
    status, detail = security.try_unlock(text_input)
    if status == "unlocked":
        reply = f"Password accepted. Welcome back, {USER_TITLE}."
        if detail:
            reply += " While I was locked: " + " ".join(detail)
        return reply, False
    if status == "blocked":
        return f"Too many wrong tries. Try again in {detail} seconds.", False
    return "That's not the password. Please try again." + (f" {detail} tries left." if detail <= 2 else ""), False


def handle_command(text_input, source="typed"):
    global _PENDING_ACTION
    user_text = text_input.lower().strip()

    if security.enabled():
        if security.is_locked():
            return _locked_reply(text_input)
        if LOCK_PHRASES.match(user_text.strip(" .!")):
            security.lock()
            return "Locked. Say the password when you need me again.", False
        security.touch()

    if _PENDING_ACTION is not None:
        if user_text == "cancel":
            _PENDING_ACTION = None
            return "Cancelled the pending action.", False
        if user_text == "confirm":
            if source != "typed":
                return "For safety, type CONFIRM in Jarvis's command box to authorize this action.", False
            pending_action = _PENDING_ACTION
            _PENDING_ACTION = None
            try:
                return app.execute_confirmed_action(pending_action), False
            except (OSError, ValueError, KeyError) as exc:
                return f"The confirmed action could not be completed: {exc}", False
        return "An action is waiting for confirmation. Type CONFIRM to proceed or CANCEL.", False

    if "i love you 3000" in user_text:
        return f"A touching sentiment, {USER_TITLE}. I shall take my leave now.", True

    if any(phrase in user_text for phrase in [
        "terminate task", "termination", "stop jarvis", "turn off jarvis", "shutdown jarvis", "goodbye jarvis"
    ]) or user_text.strip(" .!") in {"terminate", "exit", "quit", "close jarvis", "exit jarvis", "bye jarvis"}:
        return f"Understood, {USER_TITLE}. I shall take my leave now.", True

    if text_input == "WAKE_SIGNAL_CONFIRMED":
        return f"Yes, {USER_TITLE}?", False

    if text_input == "SLEEP_SIGNAL_CONFIRMED":
        return f"Certainly, {USER_TITLE}. I shall wait quietly until you need me.", False

    if user_text.startswith("the "):
        text_input = text_input[4:].strip()
    elif user_text == "the":
        return "", False

    if not text_input.strip():
        return "", False

    file_request = app.parse_text_file_request(text_input)
    if file_request:
        content = file_request["content"]
        if not content and file_request["topic"]:
            essay_request = (
                "Write a concise, factual essay about "
                f"{file_request['topic']}. Return only the essay text, without a preface or markdown fences."
            )
            content = llm.brain(essay_request)
            if content.startswith((
                "I cannot reach the local language model",
                "The local language model is unavailable",
                "Mainframe background computation hitch:",
            )):
                return content, False
        if not content:
            return "Tell me what you would like written in the text file, Sarthak.", False
        try:
            file_path = app.create_desktop_text_file(file_request["filename"], content)
            return f"Created your text file on the Desktop: {file_path}.", False
        except (OSError, ValueError) as exc:
            return f"I could not create the text file: {exc}", False

    action_response = app.trigger_action(text_input)
    if isinstance(action_response, app.ConfirmationRequest):
        _PENDING_ACTION = action_response
        return action_response.prompt, False
    if action_response == "termination":
        return f"Understood, {USER_TITLE}. I shall take my leave now.", True

    if action_response:
        return personalize_response(action_response), False

    # Not a quick command: let the agent plan it (multi-step tasks, questions, chat).
    if AGENT_ENABLED:
        response = agent.handle(text_input, source)
        if isinstance(response, app.ConfirmationRequest):
            _PENDING_ACTION = response
            return response.prompt, False
    else:
        response = llm.brain(text_input)
    return personalize_response(response), False


def main():
    if not acquire_single_instance():
        return


    print("[Startup Matrix] Phase 3: Loading desktop interface and speech listener...")
    from ears import listener
    from desktop_ui import run_ui

    run_ui(handle_command, listener, speaker.speak)
    print("[Shutdown] Jarvis window closed.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Shutdown] Jarvis core received an interrupt signal. Systems going offline cleanly.")
        sys.exit(0)
