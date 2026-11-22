import types
import unittest
from unittest.mock import MagicMock, patch

from automation import app, camera, tools


def fake_proc(name):
    return types.SimpleNamespace(info={"name": name})


class CloseAppTests(unittest.TestCase):
    def setUp(self):
        for name, value in [("IS_WINDOWS", True), ("NO_WINDOW", 0)]:
            patcher = patch.object(tools, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run = patch.object(tools.subprocess, "run").start()
        self.addCleanup(patch.stopall)

    def _processes(self, *names):
        return patch("psutil.process_iter", return_value=[fake_proc(n) for n in names])

    def test_camera_maps_to_windows_camera_process(self):
        self.run.return_value.returncode = 0
        with self._processes("WindowsCamera.exe", "chrome.exe"):
            self.assertEqual(tools.close_app("Camera"), "Closing Camera.")
        self.assertIn("WindowsCamera.exe", self.run.call_args.args[0])

    def test_camera_is_forced_if_it_ignores_a_normal_close(self):
        self.run.side_effect = [MagicMock(returncode=1), MagicMock(returncode=0)]
        with self._processes("WindowsCamera.exe"):
            self.assertEqual(tools.close_app("the camera app"), "Closed the camera app.")
        self.assertIn("/F", self.run.call_args.args[0])

    def test_unknown_app_found_by_partial_process_name(self):
        self.run.return_value.returncode = 0
        with self._processes("WhatsApp.Root.exe", "explorer.exe"):
            tools.close_app("whatsapp")
        self.assertIn("WhatsApp.Root.exe", self.run.call_args.args[0])
        self.assertNotIn("explorer.exe", self.run.call_args.args[0])

    def test_apps_with_unsaved_work_are_not_forced(self):
        self.run.return_value.returncode = 1
        with self._processes("notepad.exe"):
            reply = tools.close_app("notepad")
        self.assertIn("force close", reply)
        self.assertEqual(self.run.call_count, 1)

    def test_terminate_forces(self):
        self.run.return_value.returncode = 0
        with self._processes("notepad.exe"):
            tools.close_app("notepad", force=True)
        self.assertIn("/F", self.run.call_args.args[0])


class RecordingTests(unittest.TestCase):
    def setUp(self):
        tools._screen_recording = False
        self.addCleanup(setattr, tools, "_screen_recording", False)

    def test_start_recording_from_camera_voice_command(self):
        with patch.object(camera, "is_recording", return_value=False), \
                patch.object(camera, "start", return_value=camera.recordings_folder() / "Recording_x.mp4"), \
                patch.object(tools, "IS_WINDOWS", False):
            reply = app.trigger_action("Jarvis start the recording from my camera")
        self.assertIn("Recording from your camera", reply)

    def test_webcam_busy_message_is_spoken(self):
        with patch.object(camera, "start", side_effect=camera.CameraError("close that first")), \
                patch.object(tools, "IS_WINDOWS", False):
            self.assertEqual(app.trigger_action("record a video from my webcam"), "close that first")

    def test_stop_recording_stops_camera(self):
        path = camera.recordings_folder() / "Recording_x.mp4"
        with patch.object(camera, "stop", return_value=(path, 12.4)), patch.object(tools, "IS_WINDOWS", False):
            reply = app.trigger_action("stop recording")
        self.assertIn("Saved 12 seconds", reply)

    def test_stop_recording_with_nothing_running(self):
        with patch.object(camera, "stop", return_value=None):
            self.assertEqual(tools.stop_recording(), "Nothing is recording right now.")

    def test_camera_stop_when_idle(self):
        self.assertIsNone(camera.stop())
        self.assertFalse(camera.is_recording())


if __name__ == "__main__":
    unittest.main()
