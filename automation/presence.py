"""Whether the user is at the laptop.

"away" if you said so ("I'm going out", "I'm busy") or, in automatic mode, if the
keyboard and mouse haven't been touched for JARVIS_AWAY_AFTER_MINUTES (default 15).
"""
import ctypes
import os
import threading

_lock = threading.Lock()
_manual = None  # None = automatic, True = away, False = available


def set_away(away):
    """True = away, False = available, None = decide automatically from keyboard/mouse activity."""
    global _manual
    with _lock:
        _manual = away


def manual_setting():
    return _manual


def idle_seconds():
    """Seconds since the last keyboard or mouse input on Windows (0 elsewhere)."""
    if os.name != "nt":
        return 0.0

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(LastInputInfo)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


def is_away():
    if _manual is not None:
        return _manual
    try:
        limit = float(os.getenv("JARVIS_AWAY_AFTER_MINUTES", "15") or 0)
    except ValueError:
        limit = 15
    return limit > 0 and idle_seconds() >= limit * 60


def describe():
    if _manual is True:
        return "away"
    if _manual is False:
        return "available"
    return "away (no activity for a while)" if is_away() else "available"
