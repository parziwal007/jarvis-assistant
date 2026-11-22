import threading
import time
import unittest
from unittest.mock import patch

from mouth import speaker


class FakeCommunicate:
    log = []
    fail_voices = set()

    def __init__(self, text, voice, rate, pitch):
        self.text, self.voice = text, voice

    async def save(self, path):
        if self.voice in FakeCommunicate.fail_voices:
            raise ValueError("No audio was received")
        time.sleep(0.05)  # pretend the voice service takes a moment
        FakeCommunicate.log.append(("made", self.text, time.monotonic()))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.text)


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        cache_folder = tempfile.TemporaryDirectory()  # never touch the real voice cache
        self.addCleanup(cache_folder.cleanup)
        env = patch.dict(os.environ, {"LOCALAPPDATA": cache_folder.name})
        env.start()
        self.addCleanup(env.stop)
        FakeCommunicate.log = []
        FakeCommunicate.fail_voices = set()
        self.played = []

        def fake_read(path, dtype=None):
            with open(path, encoding="utf-8") as handle:
                return handle.read(), 24000

        def fake_play(audio, rate):
            self.played.append((audio, time.monotonic()))
            time.sleep(0.1)  # playback takes longer than generating the next sentence

        fake_edge = type("E", (), {"Communicate": FakeCommunicate})
        for target, value in [("edge_tts", fake_edge)]:
            patcher = patch.object(speaker, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in [("read", fake_read)]:
            patcher = patch.object(speaker.sf, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in [("play", fake_play), ("wait", lambda: None)]:
            patcher = patch.object(speaker.sd, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_splits_into_natural_chunks(self):
        self.assertEqual(
            speaker.split_sentences("Hi. You have two emails. One is from Rahul about a call at six. Want me to reply?"),
            ["Hi. You have two emails. One is from Rahul about a call at six.", "Want me to reply?"],
        )

    def test_plays_in_order_and_prepares_next_sentence_during_playback(self):
        text = ("You have two new emails this afternoon. The first is from Rahul about the project call. "
                "The second is a LinkedIn job alert for a backend role. Do you want me to reply to Rahul now?")
        speaker._speak_neural(text, voice_name="en-US-AvaMultilingualNeural")
        chunks = speaker.split_sentences(text)
        self.assertEqual([audio for audio, _ in self.played], chunks)
        second_made = next(t for kind, chunk, t in FakeCommunicate.log if chunk == chunks[1])
        second_played = self.played[1][1]
        first_played = self.played[0][1]
        self.assertLess(second_made, first_played + 0.1)  # ready before the first sentence finished
        self.assertGreaterEqual(second_played, first_played + 0.1)

    def test_short_phrases_are_cached_after_first_time(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LOCALAPPDATA": folder}):
            self.assertFalse(os.listdir(folder))
            speaker.warm_cache(["Yes, Sarthak?"])
            made_before = len(FakeCommunicate.log)
            speaker._speak_neural("Yes, Sarthak?")
            speaker._speak_neural("Yes, Sarthak?")
            self.assertEqual(len(FakeCommunicate.log), made_before)  # no new network calls
            self.assertEqual([audio for audio, _ in self.played], ["Yes, Sarthak?", "Yes, Sarthak?"])

    def test_falls_back_to_guy_voice(self):
        FakeCommunicate.fail_voices = {"en-US-AndrewMultilingualNeural"}
        speaker._speak_neural("Hello there, this is a test sentence for Jarvis.",
                              voice_name="en-US-AndrewMultilingualNeural")
        self.assertEqual(len(self.played), 1)


if __name__ == "__main__":
    unittest.main()
