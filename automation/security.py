"""Password lock for Jarvis.

Set a password with:  python set_password.py
Jarvis then starts locked and only answers after the password is said or typed.
Only a salted hash of the password is stored (JARVIS_PASSWORD_HASH in .env), never the password itself.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

ITERATIONS = 200_000
MAX_TRIES = 5
FIRST_LOCKOUT_SECONDS = 60

NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "for": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}
LEAD_INS = re.compile(
    r"^(?:(?:hey |ok |okay )?jarvis[ ,]+)?(?:(?:the |my )?(?:password|passcode|pass code|code) is |"
    r"(?:the |my )?(?:password|passcode)[ ,:]+|unlock(?: with)? )?"
)

_lock = threading.Lock()
_state = {"locked": True, "last_used": time.monotonic(), "fails": 0, "blocked_until": 0.0, "lockouts": 0}
_missed = []


def normalize(text):
    """Makes a spoken or typed password comparable however Whisper writes it.

    Case and punctuation are ignored, a lead-in like "the password is" is dropped, and digits said one
    by one become numbers: "Blue mango, four two." and "blue mango 42" are the same password."""
    text = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    text = LEAD_INS.sub("", text).strip()
    words = [NUMBER_WORDS.get(word, word) for word in text.split()]
    joined = []
    for word in words:  # "1 2 3 4" -> "1234"
        if joined and word.isdigit() and joined[-1].isdigit():
            joined[-1] += word
        else:
            joined.append(word)
    return " ".join(joined)


def _key(text):
    # Spaces don't count either: Whisper may write "iron man" or "ironman".
    return normalize(text).replace(" ", "").encode()


def make_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", _key(password), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def _stored():
    return os.getenv("JARVIS_PASSWORD_HASH", "").strip()


def enabled():
    return bool(_stored())


def matches(text, stored=None):
    stored = stored or _stored()
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", _key(text), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def lock_after_seconds():
    try:
        minutes = float(os.getenv("JARVIS_LOCK_AFTER_MINUTES", "15") or 15)
    except ValueError:
        minutes = 15
    return minutes * 60


def is_locked():
    """True while Jarvis needs the password. Also locks itself after a while unused."""
    if not enabled():
        return False
    with _lock:
        limit = lock_after_seconds()
        if not _state["locked"] and limit > 0 and time.monotonic() - _state["last_used"] > limit:
            _state["locked"] = True
        return _state["locked"]


def lock():
    with _lock:
        _state["locked"] = True


def touch():
    with _lock:
        _state["last_used"] = time.monotonic()


def try_unlock(text):
    """Returns ("unlocked", missed_messages), ("wrong", tries_left) or ("blocked", seconds_left)."""
    with _lock:
        now = time.monotonic()
        if now < _state["blocked_until"]:
            return "blocked", int(_state["blocked_until"] - now) + 1
    ok = matches(text)
    with _lock:
        if ok:
            _state.update({"locked": False, "last_used": time.monotonic(), "fails": 0, "lockouts": 0})
            missed = list(_missed)
            _missed.clear()
            return "unlocked", missed
        _state["fails"] += 1
        if _state["fails"] >= MAX_TRIES:
            seconds = FIRST_LOCKOUT_SECONDS * (2 ** _state["lockouts"])
            _state["lockouts"] += 1
            _state["fails"] = 0
            _state["blocked_until"] = time.monotonic() + seconds
            return "blocked", seconds
        return "wrong", MAX_TRIES - _state["fails"]


def hold_message(message):
    """Keeps a notification (mail alert, reminder) private until Jarvis is unlocked."""
    with _lock:
        _missed.append(message)


def hide(text):
    """What to print or show instead of something said while locked (it may be the password)."""
    return "(hidden while Jarvis is locked)" if is_locked() else text


def _reset_for_tests():
    with _lock:
        _state.update({"locked": True, "last_used": time.monotonic(), "fails": 0, "blocked_until": 0.0, "lockouts": 0})
        _missed.clear()
