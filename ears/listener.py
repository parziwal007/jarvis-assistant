"""Wake-word listening and speech-to-text for Jarvis.

How it works:
1. The microphone is read in 0.1 s blocks. Jarvis measures the room's background
   noise and treats anything clearly louder as speech. A sentence ends after a
   short pause (or after 15 s at most), so background noise can't keep a command
   "open" forever.
2. Each finished sentence is transcribed with Whisper (faster-whisper, offline after
   a one-time model download). If faster-whisper isn't installed, the Vosk model in
   the "model" folder is used instead.
3. While asleep, sentences without "Jarvis" are ignored. "Jarvis" alone wakes it;
   "Jarvis, open YouTube" wakes it and runs the command. After that, follow-up
   commands work without "Jarvis" for JARVIS_AWAKE_SECONDS.
4. The microphone is muted while Jarvis speaks, so it never hears itself.

Useful .env settings:
  JARVIS_WHISPER_MODEL   tiny.en | base.en (default) | small.en (more accurate, slower)
  JARVIS_SPEECH_THRESHOLD  fixed loudness threshold if auto-calibration misbehaves
  JARVIS_INPUT_DEVICE    microphone number
  JARVIS_DEBUG_AUDIO=1   print loudness levels to tune the threshold
"""
import collections
import json
import os
import queue
import re
import sys
import threading
import time

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
BLOCK_SAMPLES = int(SAMPLE_RATE * BLOCK_SECONDS)
PRE_ROLL_BLOCKS = 4  # keep 0.4 s before speech starts so the first word isn't clipped
END_SILENCE_BLOCKS = max(3, int(float(os.getenv("JARVIS_SILENCE_TIMEOUT", "1.0")) / BLOCK_SECONDS))
MAX_UTTERANCE_BLOCKS = int(15 / BLOCK_SECONDS)
MIN_VOICED_BLOCKS = 3  # ignore clicks and coughs shorter than 0.3 s
MIN_THRESHOLD = 150.0
NOISE_FACTOR = 3.0
AWAKE_WINDOW = float(os.getenv("JARVIS_AWAKE_SECONDS", "180"))  # 0 = stay awake until told to sleep
CONTINUATION_SECONDS = 5.0  # how long to wait for the rest of "write an essay about ..."
DEBUG_AUDIO = os.getenv("JARVIS_DEBUG_AUDIO", "").strip() in {"1", "true", "yes"}
FIXED_THRESHOLD = os.getenv("JARVIS_SPEECH_THRESHOLD", "").strip()

WAKE_WORDS = r"(?:hey |hi |hello |ok |okay )?(?:jarvis|jervis|charvis|jarvas|javis)\b[\s,]*"
WAKE_AT_START = re.compile(r"^" + WAKE_WORDS, re.IGNORECASE)
WAKE_ANYWHERE = re.compile(r"^(?:.*?\b)?" + WAKE_WORDS, re.IGNORECASE)
WAKE_ONLY_PHRASES = {"wake up", "jarvis wake up", "hey jarvis", "hello jarvis"}
SLEEP_KEYWORDS = ["standby", "go to sleep", "stop listening", "good night", "sleep now", "go offline"]
TERMINATION_KEYWORDS = ["terminate task", "termination"]
TERMINATION_PHRASES = {  # whole commands that close Jarvis
    "terminate", "exit", "quit", "close jarvis", "exit jarvis", "quit jarvis", "shut down jarvis",
    "shutdown jarvis", "turn off jarvis", "stop jarvis", "goodbye jarvis", "bye jarvis",
}
# Phrases Whisper tends to invent from silence or noise.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you for watching", "thanks for watching", "bye",
    "the", "uh", "um", "hmm", "okay", "so", "i", "oh",
}
# A command ending in one of these words is unfinished: wait for the rest.
DANGLING_WORDS = {
    "about", "and", "to", "the", "a", "an", "of", "for", "with", "on", "in", "into", "at", "from",
    "by", "or", "but", "that", "my", "your", "this", "some", "is", "called", "named", "saying",
    "write", "open", "search", "play", "set", "type",
}
WHISPER_PROMPT = ("Jarvis, open Notepad and write an essay. Jarvis, set the volume to 40. Open YouTube. "
                  "Summarize my mails. Check my emails.")
# Names Whisper should expect (artists, friends, apps), comma separated, e.g. "Karan Aujla, Priya, Rahul".
_EXTRA_WORDS = os.getenv("JARVIS_WHISPER_WORDS", "").strip()
if _EXTRA_WORDS:
    WHISPER_PROMPT += " Names: " + _EXTRA_WORDS + "."

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

try:
    from automation import security as _security
except ImportError:  # running the listener on its own
    class _security:  # noqa: N801
        hide = staticmethod(lambda text: text)

audio_queue = queue.Queue()
_muted = threading.Event()
_busy = threading.Event()
IS_JARVIS_AWAKE = False
AWAKE_UNTIL = 0.0
_noise_level = None


# ---------------------------------------------------------------------------
# Small helpers (also used by tests)
# ---------------------------------------------------------------------------
def set_muted(muted):
    """Called by the UI around speech so Jarvis's own voice is not transcribed."""
    global AWAKE_UNTIL
    if muted:
        _muted.set()
    else:
        _muted.clear()
        if IS_JARVIS_AWAKE:  # long replies shouldn't use up the follow-up window
            AWAKE_UNTIL = time.time() + AWAKE_WINDOW


def set_busy(busy):
    """Called by the UI while a command runs, so Jarvis doesn't fall asleep mid-task."""
    global AWAKE_UNTIL
    if busy:
        _busy.set()
    else:
        _busy.clear()
        if IS_JARVIS_AWAKE:
            AWAKE_UNTIL = time.time() + AWAKE_WINDOW


def wake_up():
    """Starts listening for a command without needing "Jarvis" first (e.g. to hear the password)."""
    global IS_JARVIS_AWAKE, AWAKE_UNTIL
    IS_JARVIS_AWAKE = True
    AWAKE_UNTIL = time.time() + AWAKE_WINDOW


def is_unfinished(command):
    words = command.split()
    return bool(words) and words[-1] in DANGLING_WORDS


def is_awake():
    return IS_JARVIS_AWAKE


def normalize_transcript(text):
    """'Jarvis, open YouTube.' -> 'jarvis open youtube'"""
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s'@.%+-]", " ", text)
    text = re.sub(r"(?<!\w)[.]|[.](?!\w)", " ", text)  # drop sentence dots, keep dots in emails/URLs
    return " ".join(text.split())


def split_wake_phrase(text, anywhere=False):
    """Returns (woke, command_after_wake_word).

    "jarvis open notepad" -> (True, "open notepad"); "jarvis" -> (True, "");
    "open notepad" -> (False, "open notepad"). With anywhere=True the wake word may
    come after filler ("uh hey jarvis open notepad").
    """
    cleaned = normalize_transcript(text)
    if cleaned in WAKE_ONLY_PHRASES:
        return True, ""
    match = (WAKE_ANYWHERE if anywhere else WAKE_AT_START).match(cleaned)
    if match and match.group(0).strip():
        return True, cleaned[match.end():].strip()
    return False, cleaned


def _contains(text, keywords):
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def block_rms(block):
    samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


class UtteranceSegmenter:
    """Splits a stream of 0.1 s audio blocks into utterances using loudness."""

    def __init__(self, noise_level=None):
        self.noise = noise_level
        self.pre_roll = collections.deque(maxlen=PRE_ROLL_BLOCKS)
        self.reset()

    def reset(self):
        self.in_speech = False
        self.blocks = []
        self.voiced = 0
        self.silence = 0
        self.pre_roll.clear()

    def threshold(self):
        if FIXED_THRESHOLD:
            try:
                return float(FIXED_THRESHOLD)
            except ValueError:
                pass
        return max(MIN_THRESHOLD, (self.noise or MIN_THRESHOLD) * NOISE_FACTOR)

    def feed(self, block):
        """Returns the utterance audio (bytes) when one just ended, else None."""
        rms = block_rms(block)
        if self.noise is None:
            self.noise = rms
        threshold = self.threshold()
        loud = rms > threshold
        if DEBUG_AUDIO:
            print(f"[Audio] level {rms:7.0f}  threshold {threshold:6.0f}  {'SPEECH' if loud else ''}")

        if not self.in_speech:
            # Track the background level only while nobody is talking.
            self.noise = 0.9 * self.noise + 0.1 * rms
            if loud:
                self.in_speech = True
                self.blocks = [*self.pre_roll, block]
                self.voiced, self.silence = 1, 0
            else:
                self.pre_roll.append(block)
            return None

        self.blocks.append(block)
        if loud:
            self.voiced += 1
            self.silence = 0
        else:
            self.silence += 1
        if self.silence >= END_SILENCE_BLOCKS or len(self.blocks) >= MAX_UTTERANCE_BLOCKS:
            audio, voiced = b"".join(self.blocks), self.voiced
            self.reset()
            if voiced >= MIN_VOICED_BLOCKS:
                return audio
        return None


# ---------------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------------
def get_input_device_index():
    """Prefer the system default input, with an optional numeric device override."""
    try:
        devices = sd.query_devices()
    except Exception:
        return None

    configured_device = os.getenv("JARVIS_INPUT_DEVICE", "").strip()
    if configured_device:
        try:
            device_index = int(configured_device)
            if devices[device_index].get("max_input_channels", 0) > 0:
                return device_index
        except (ValueError, IndexError, TypeError):
            print(f"[Audio] Ignoring invalid JARVIS_INPUT_DEVICE: {configured_device}")

    default_device = getattr(sd.default, "device", None)
    if isinstance(default_device, (list, tuple)) and default_device:
        try:
            default_index = int(default_device[0])
            if default_index >= 0 and devices[default_index].get("max_input_channels", 0) > 0:
                return default_index
        except (TypeError, ValueError, IndexError):
            pass

    for idx, device in enumerate(devices):
        if device.get("max_input_channels", 0) <= 0:
            continue
        name = str(device.get("name", "")).lower()
        if any(term in name for term in ("microphone", "mic", "headset", "input")):
            return idx
    return None


_last_level = 0.0


def audio_callback(indata, frames, time_info, status):
    global _last_level
    block = None if _muted.is_set() else bytes(indata)
    audio_queue.put(block)
    try:
        _last_level = block_rms(block) if block else 0.0
    except Exception:
        _last_level = 0.0


def mic_level():
    """Loudness of the microphone right now, 0.0 (silence) to 1.0 (loud), for the on-screen meter."""
    return max(0.0, min(1.0, _last_level / 4000.0))


def _drain_audio():
    while True:
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            return


# ---------------------------------------------------------------------------
# Speech-to-text engines
# ---------------------------------------------------------------------------
class WhisperEngine:
    name = "whisper"

    def __init__(self):
        from faster_whisper import WhisperModel

        model_name = os.getenv("JARVIS_WHISPER_MODEL", "base.en").strip() or "base.en"
        print(f"[Audio] Loading Whisper '{model_name}' (first run downloads it once)...")
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")
        self.model_name = model_name
        # 1 = fastest; 5 = a little more accurate but 2-3x slower on a laptop CPU
        self.beam_size = max(1, int(os.getenv("JARVIS_WHISPER_BEAM", "1") or 1))

    def transcribe(self, audio_bytes):
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=self.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=WHISPER_PROMPT,
            without_timestamps=True,
        )
        parts = [segment.text for segment in segments if segment.no_speech_prob < 0.6]
        return " ".join(parts).strip()


class VoskEngine:
    name = "vosk"

    def __init__(self, model_path):
        from vosk import KaldiRecognizer, Model

        self.model = Model(model_path)
        self.recognizer_class = KaldiRecognizer

    def transcribe(self, audio_bytes):
        recognizer = self.recognizer_class(self.model, SAMPLE_RATE)
        recognizer.AcceptWaveform(audio_bytes)
        return json.loads(recognizer.FinalResult()).get("text", "").strip()


def _load_engine():
    try:
        return WhisperEngine()
    except ImportError:
        print("[Audio] faster-whisper is not installed; using Vosk (less accurate). "
              "Run: python -m pip install faster-whisper")
    except Exception as exc:
        print(f"[Audio] Whisper could not load ({exc}); using Vosk instead.")
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    model_path = os.path.join(base_dir, "model")
    if not os.path.exists(model_path):
        print(f"[Audio] No speech engine available: install faster-whisper or put a Vosk model in {model_path}")
        sys.exit(1)
    return VoskEngine(model_path)


INPUT_DEVICE_INDEX = get_input_device_index()
try:
    input_device = sd.query_devices(INPUT_DEVICE_INDEX, "input")
    print(f"[Audio] Using microphone: {input_device['name']} (device {INPUT_DEVICE_INDEX})")
except Exception as exc:
    print(f"[Audio] Using system-default microphone; device lookup failed: {exc}")

engine = _load_engine()
print(f"[Audio] Speech recognition: {engine.name}")


# ---------------------------------------------------------------------------
# Main entry point used by the UI
# ---------------------------------------------------------------------------
def _handle_transcript(text):
    """Decides what a transcribed sentence means. Returns a result for listen() or None to keep listening."""
    global IS_JARVIS_AWAKE, AWAKE_UNTIL

    cleaned = normalize_transcript(text)
    if cleaned in HALLUCINATIONS:
        return None
    print(f"[Audio] Heard: {_security.hide(cleaned)}")

    addressed, without_wake = split_wake_phrase(cleaned)
    if _contains(cleaned, TERMINATION_KEYWORDS) or (
        without_wake in TERMINATION_PHRASES and (addressed or IS_JARVIS_AWAKE)
    ):
        return "termination"

    if not IS_JARVIS_AWAKE:
        woke, command = split_wake_phrase(cleaned, anywhere=True)
        if not woke:
            return None  # not addressed to Jarvis
        IS_JARVIS_AWAKE = True
        AWAKE_UNTIL = time.time() + AWAKE_WINDOW
        if not command:
            return "WAKE_SIGNAL_CONFIRMED"
    else:
        woke, command = split_wake_phrase(cleaned)
        if woke and not command:
            AWAKE_UNTIL = time.time() + AWAKE_WINDOW
            return "WAKE_SIGNAL_CONFIRMED"  # "Jarvis?" again while already awake

    if _contains(command, SLEEP_KEYWORDS):
        IS_JARVIS_AWAKE = False
        return "SLEEP_SIGNAL_CONFIRMED"
    if command in HALLUCINATIONS:
        return None
    AWAKE_UNTIL = time.time() + AWAKE_WINDOW
    return command


CONTROL_SIGNALS = {"termination", "WAKE_SIGNAL_CONFIRMED", "SLEEP_SIGNAL_CONFIRMED"}


def listen():
    """Blocks until there is something for Jarvis to handle and returns it.

    Returns a command string, "termination", "WAKE_SIGNAL_CONFIRMED" (wake word
    alone) or "SLEEP_SIGNAL_CONFIRMED".
    """
    global IS_JARVIS_AWAKE, _noise_level

    _drain_audio()
    segmenter = UtteranceSegmenter(_noise_level)
    pending, pending_until = "", 0.0
    if not IS_JARVIS_AWAKE:
        print("[Audio] Listening for 'Jarvis'.")

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=BLOCK_SAMPLES,
        device=INPUT_DEVICE_INDEX,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        try:
            while True:
                now = time.time()
                if pending and now > pending_until and not segmenter.in_speech:
                    return pending  # the rest never came; use what we have
                if (IS_JARVIS_AWAKE and AWAKE_WINDOW > 0 and now > AWAKE_UNTIL
                        and not segmenter.in_speech and not _busy.is_set()):
                    IS_JARVIS_AWAKE = False
                    print("[Audio] No command for a while; waiting for 'Jarvis' again.")

                try:
                    block = audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if block is None:  # Jarvis is speaking
                    segmenter.reset()
                    continue

                utterance = segmenter.feed(block)
                if utterance is None:
                    continue
                try:
                    started = time.time()
                    text = engine.transcribe(utterance)
                    print(f"[Timing] heard {len(utterance) / 2 / SAMPLE_RATE:.1f}s of speech, "
                          f"transcribed in {time.time() - started:.1f}s")
                except Exception as exc:
                    print(f"[Audio] Transcription failed: {exc}")
                    continue
                if pending:
                    _, rest = split_wake_phrase(text)
                    result = f"{pending} {rest}".strip() if rest and rest not in HALLUCINATIONS else None
                    if result:
                        print(f"[Audio] Joined: {_security.hide(result)}")
                    else:
                        continue
                else:
                    result = _handle_transcript(text)
                if result is None:
                    continue
                if result not in CONTROL_SIGNALS and is_unfinished(result):
                    pending, pending_until = result, time.time() + CONTINUATION_SECONDS
                    print("[Audio] Sounds unfinished; waiting for the rest...")
                    continue
                return result
        finally:
            _noise_level = segmenter.noise
