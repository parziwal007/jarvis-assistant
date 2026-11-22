import asyncio
import os
import queue
import re
import threading
import subprocess
import sys
import tempfile

import sounddevice as sd
import soundfile as sf

try:
    import edge_tts
except ImportError:
    edge_tts = None


def sanitize_text(text):
    """Normalize whitespace before passing speech text to the synthesizer."""
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ")
    return " ".join(text.split()).strip()


FALLBACK_VOICE = "en-US-GuyNeural"


def voice_settings():
    """Voice, speed and pitch from .env (JARVIS_NEURAL_VOICE, JARVIS_VOICE_RATE, JARVIS_VOICE_PITCH)."""
    voice = os.getenv("JARVIS_NEURAL_VOICE", "").strip() or "en-US-AvaMultilingualNeural"
    rate = os.getenv("JARVIS_VOICE_RATE", "").strip() or "+0%"
    pitch = os.getenv("JARVIS_VOICE_PITCH", "").strip() or "+0Hz"
    return voice, rate, pitch


async def _save_neural_speech(text, output_path, voice_name, rate, pitch):
    communicate = edge_tts.Communicate(text, voice_name, rate=rate, pitch=pitch)
    await communicate.save(output_path)


CACHE_MAX_CHARS = 90  # short, frequently repeated phrases ("Yes, Sarthak?", "Turning the volume up.")


def _cache_path(text, voice_name, rate, pitch):
    import hashlib

    base = os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    key = hashlib.sha1(f"{voice_name}|{rate}|{pitch}|{text}".encode("utf-8")).hexdigest()
    return os.path.join(base, "JarvisAssistant", "voice_cache", f"{key}.mp3")


def _synthesize(text, voice_name, rate, pitch):
    """Returns the path of a temporary mp3 with the spoken text (from the cache when possible)."""
    import shutil

    cache = _cache_path(text, voice_name, rate, pitch) if len(text) <= CACHE_MAX_CHARS else None
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as audio_file:
        audio_path = audio_file.name
    if cache and os.path.isfile(cache) and os.path.getsize(cache) > 0:
        shutil.copyfile(cache, audio_path)
        return audio_path
    path = _synthesize_online(text, audio_path, voice_name, rate, pitch)
    if cache:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            shutil.copyfile(path, cache)
        except OSError:
            pass
    return path


def warm_cache(phrases):
    """Pre-generates phrases Jarvis says often, so they play instantly later. Safe to run in the background."""
    if edge_tts is None:
        return
    voice_name, rate, pitch = voice_settings()
    for phrase in phrases:
        try:
            _remove(_synthesize(sanitize_text(phrase), voice_name, rate, pitch))
        except Exception:
            pass


def _synthesize_online(text, audio_path, voice_name, rate, pitch):
    try:
        asyncio.run(asyncio.wait_for(_save_neural_speech(text, audio_path, voice_name, rate, pitch), timeout=30))
    except Exception as exc:
        if voice_name == FALLBACK_VOICE:
            _remove(audio_path)
            raise
        print(f"[Speech] Voice {voice_name} failed ({exc}); using {FALLBACK_VOICE}.")
        try:
            asyncio.run(asyncio.wait_for(_save_neural_speech(text, audio_path, FALLBACK_VOICE, rate, pitch), timeout=30))
        except Exception:
            _remove(audio_path)
            raise
    return audio_path


def _play(audio_path):
    try:
        audio, sample_rate = sf.read(audio_path, dtype="float32")
        sd.play(audio, sample_rate)
        sd.wait()
    finally:
        _remove(audio_path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def split_sentences(text, min_chars=40):
    """Splits text into speakable chunks; very short sentences are joined to the next one."""
    pieces = [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", text) if piece.strip()]
    chunks, current = [], ""
    for piece in pieces:
        current = f"{current} {piece}".strip()
        if len(current) >= min_chars:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks or [text]


def _speak_neural(text, voice_name=None, rate=None, pitch=None):
    """Speaks text, generating the next sentence while the current one plays (much less waiting)."""
    if edge_tts is None:
        raise RuntimeError("edge-tts is not installed")
    default_voice, default_rate, default_pitch = voice_settings()
    voice_name, rate, pitch = voice_name or default_voice, rate or default_rate, pitch or default_pitch

    chunks = split_sentences(text)
    first = _synthesize(chunks[0], voice_name, rate, pitch)  # errors here fall back to the offline voice
    if len(chunks) == 1:
        _play(first)
        return

    ready = queue.Queue(maxsize=2)

    def produce():
        for chunk in chunks[1:]:
            try:
                ready.put(_synthesize(chunk, voice_name, rate, pitch))
            except Exception as exc:
                print(f"[Speech] Skipped part of the reply: {exc}")
                break
        ready.put(None)

    threading.Thread(target=produce, daemon=True).start()
    _play(first)
    while True:
        path = ready.get()
        if path is None:
            break
        _play(path)


def _speak_windows(text):
    command = (
        f'Add-Type -AssemblyName System.speech; '
        f'$text = [Console]::In.ReadToEnd(); '
        f'$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
        f'$voiceName = $env:JARVIS_VOICE; '
        f'if (-not $voiceName) {{ $voiceName = "Microsoft David Desktop" }}; '
        f'try {{ $synth.SelectVoice($voiceName) }} catch {{ }}; '
        f'$synth.Rate = -1; '
        f'$synth.Volume = 100; '
        f'$synth.Speak($text)'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        input=text,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:300])


def speak(text):
    """Speak with an American neural voice, falling back to Windows SAPI offline."""
    clean_phrase = sanitize_text(text)
    if not clean_phrase:
        return

    try:
        _speak_neural(clean_phrase)
        return
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"[Speech] Neural voice unavailable; using offline voice: {exc}")

    try:
        _speak_windows(clean_phrase)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"[Speech] Could not start Windows speech synthesis: {exc}")
