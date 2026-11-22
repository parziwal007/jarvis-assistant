import subprocess
from pathlib import Path

from pywinauto import Application


def open_in_notepad(path=None):
    command = ["notepad.exe"]
    if path is not None:
        command.append(str(Path(path)))

    application = Application(backend="uia").start(
        subprocess.list2cmdline(command),
        wait_for_idle=False,
    )
    window = application.top_window()
    window.wait("visible", timeout=10)
    window.set_focus()
