"""Listen to different voices for Jarvis and pick the one you like.

Run from the Jarvis_Assistant folder:   python voice_test.py
Then put your favourite in .env, for example:
    JARVIS_NEURAL_VOICE=en-US-AndrewMultilingualNeural
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mouth import speaker  # noqa: E402

SAMPLE = (
    "Hey Sarthak, you've got two new emails. One's from Rahul asking about a call this evening. "
    "Want me to reply and say six works?"
)
VOICES = [
    ("en-US-AndrewMultilingualNeural", "American man, warm and relaxed"),
    ("en-US-BrianMultilingualNeural", "American man, casual and friendly"),
    ("en-US-ChristopherNeural", "American man, calm and deep"),
    ("en-GB-RyanNeural", "British man, the classic movie-Jarvis feel"),
    ("en-GB-ThomasNeural", "British man, softer"),
    ("en-IN-PrabhatNeural", "Indian English, man"),
    ("en-US-AvaMultilingualNeural", "American woman, expressive (default)"),
    ("en-IN-NeerjaNeural", "Indian English, woman"),
    ("en-US-GuyNeural", "the old voice, for comparison"),
]


def main():
    print("Playing each voice. Press Ctrl+C to stop.\n")
    for number, (voice, description) in enumerate(VOICES, start=1):
        print(f"{number}. {voice}  -  {description}")
        try:
            speaker._speak_neural(SAMPLE, voice_name=voice)
        except Exception as exc:
            print(f"   (couldn't play this one: {exc})")
    print("\nPut the one you like in .env, e.g.  JARVIS_NEURAL_VOICE=en-GB-RyanNeural")
    print("Optional: JARVIS_VOICE_RATE=+5% (faster) or -5% (slower), JARVIS_VOICE_PITCH=-2Hz (deeper)")


if __name__ == "__main__":
    main()
