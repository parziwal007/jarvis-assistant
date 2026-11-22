"""Webcam video recording for Jarvis ("start recording from my camera" / "stop recording").

Records video (no sound) from the laptop's webcam with OpenCV into Videos\\Jarvis Recordings,
showing a small preview window while it records.
"""
import datetime
import os
import threading
import time
from pathlib import Path

PREVIEW_TITLE = "Jarvis camera - recording (say 'stop recording')"
MAX_MINUTES = float(os.getenv("JARVIS_CAMERA_MAX_MINUTES", "30") or 30)

_state = {"thread": None, "stop": None, "path": None, "error": None, "started": None}
_lock = threading.Lock()


class CameraError(Exception):
    pass


def recordings_folder():
    return Path.home() / "Videos" / "Jarvis Recordings"


def is_recording():
    thread = _state["thread"]
    return thread is not None and thread.is_alive()


def _open_camera(cv2):
    index = int(os.getenv("JARVIS_CAMERA_INDEX", "0") or 0)
    if os.name == "nt":
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)  # DirectShow opens laptop webcams fastest
        if not capture.isOpened():
            capture = cv2.VideoCapture(index)
    else:
        capture = cv2.VideoCapture(index)
    return capture


def _record(path, stop, ready):
    try:
        import cv2
    except ImportError:
        _state["error"] = "Camera recording needs OpenCV. Run: python -m pip install opencv-python"
        ready.set()
        return
    capture = _open_camera(cv2)
    if not capture.isOpened():
        _state["error"] = ("I couldn't open the webcam. If the Camera app or a video call is using it, "
                           "close that first.")
        ready.set()
        return
    ok, frame = capture.read()
    if not ok or frame is None:
        capture.release()
        _state["error"] = "The webcam opened but sent no picture."
        ready.set()
        return
    height, width = frame.shape[:2]
    fps = capture.get(cv2.CAP_PROP_FPS) or 0
    if not 5 <= fps <= 60:
        fps = 20.0
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        capture.release()
        _state["error"] = "I couldn't create the video file."
        ready.set()
        return
    ready.set()
    started = time.monotonic()
    frame_gap = 1.0 / fps
    next_frame = started
    try:
        while not stop.is_set() and time.monotonic() - started < MAX_MINUTES * 60:
            ok, frame = capture.read()
            if not ok:
                break
            # Write as many copies as real time needs, so the video doesn't play too fast.
            now = time.monotonic()
            while next_frame <= now:
                writer.write(frame)
                next_frame += frame_gap
            try:
                cv2.imshow(PREVIEW_TITLE, frame)
                cv2.waitKey(1)
            except cv2.error:
                pass  # no preview available; keep recording
    finally:
        capture.release()
        writer.release()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


def start():
    """Starts recording. Returns the file path. Raises CameraError if the webcam can't be used."""
    with _lock:
        if is_recording():
            raise CameraError("I'm already recording from the camera. Say 'stop recording' to finish.")
        folder = recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"Recording_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
        stop, ready = threading.Event(), threading.Event()
        _state.update({"stop": stop, "path": path, "error": None, "started": time.monotonic()})
        thread = threading.Thread(target=_record, args=(path, stop, ready), daemon=True, name="JarvisCamera")
        _state["thread"] = thread
        thread.start()
    if not ready.wait(10):
        stop.set()
        raise CameraError("The webcam didn't respond.")
    if _state["error"]:
        _state["thread"] = None
        raise CameraError(_state["error"])
    return path


def stop():
    """Stops recording. Returns (path, seconds) or None if nothing was recording."""
    with _lock:
        thread, event, path = _state["thread"], _state["stop"], _state["path"]
        if thread is None or not thread.is_alive():
            _state["thread"] = None
            return None
        event.set()
    thread.join(10)
    seconds = time.monotonic() - (_state["started"] or time.monotonic())
    _state["thread"] = None
    return path, seconds
