import hashlib
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing


DEFAULT_MODELS = [
    "qwen2.5:7b-instruct",
    "llama3.2",
    "llama3",
]
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_XAI_MODEL = "grok-4.7"
DEFAULT_OPENROUTER_MODEL = "x-ai/grok-4.7"
OPENAI_COMPATIBLE_PROVIDERS = {
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "JARVIS_OPENROUTER_MODEL",
        "default_model": DEFAULT_OPENROUTER_MODEL,
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "probe_url": "https://openrouter.ai",
    },
    "xai": {
        "key_env": "XAI_API_KEY",
        "model_env": "JARVIS_XAI_MODEL",
        "default_model": DEFAULT_XAI_MODEL,
        "endpoint": "https://api.x.ai/v1/chat/completions",
        "probe_url": "https://api.x.ai",
    },
}
SUPPORTED_CLOUD_PROVIDERS = ("openrouter", "xai", "gemini")
CONVERSATION_HISTORY = []
MAX_HISTORY_MESSAGES = 8
CACHE_TTL_SECONDS = 600
CACHE_MAX_ENTRIES = 128


def _int_env(name, default):
    try:
        return max(64, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


# 160 tokens was too small: Gemini 3.x "thinking" tokens count against this budget,
# which produced empty or cut-off replies. Override with JARVIS_MAX_TOKENS if needed.
CHAT_MAX_TOKENS = _int_env("JARVIS_MAX_TOKENS", 1024)
LONG_TEXT_MAX_TOKENS = _int_env("JARVIS_LONG_TEXT_MAX_TOKENS", 2048)
OLLAMA_CONTEXT_TOKENS = _int_env("JARVIS_OLLAMA_CONTEXT", 8192)
SYSTEM_PROMPT = (
    "You are Jarvis, a capable personal desktop assistant. Be confident, clear, warm, and "
    "practical, with subtle wit when appropriate. Address the user as Sarthak. Listen to the "
    "user's intent, use recent conversation context, and ask one concise clarifying question "
    "when needed. Be honest about uncertainty and never claim an action succeeded unless the "
    "app confirms it. Keep ordinary replies concise, but provide detail when asked."
)

logger = logging.getLogger(__name__)


def is_ollama_available():
    """Checks whether the local Ollama server is up and listening."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


_REACHABILITY = {}  # provider -> (reachable, checked_at)


def is_internet_available(provider="gemini"):
    """Checks whether the provider's host is reachable, remembering the answer briefly
    (60 s when online, 15 s when offline) so every request doesn't pay for a network check."""
    cached = _REACHABILITY.get(provider)
    if cached and time.time() - cached[1] < (60 if cached[0] else 15):
        return cached[0]
    reachable = _probe(provider)
    _REACHABILITY[provider] = (reachable, time.time())
    return reachable


def mark_unreachable(provider):
    _REACHABILITY[provider] = (False, time.time())


def _probe(provider):
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        probe_url = OPENAI_COMPATIBLE_PROVIDERS[provider]["probe_url"]
    else:
        probe_url = "https://generativelanguage.googleapis.com"
    try:
        with urllib.request.urlopen(probe_url, timeout=2) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def get_cloud_provider_candidates():
    """Returns configured cloud providers in deterministic priority order."""
    configured_choice = os.getenv("JARVIS_CLOUD_PROVIDER", "auto").strip().lower() or "auto"
    if configured_choice != "auto" and configured_choice not in SUPPORTED_CLOUD_PROVIDERS:
        logger.warning("Unsupported JARVIS_CLOUD_PROVIDER value: %s", configured_choice)
        return []

    providers = [configured_choice] if configured_choice != "auto" else SUPPORTED_CLOUD_PROVIDERS
    ready = []
    for provider in providers:
        key_name = (
            OPENAI_COMPATIBLE_PROVIDERS[provider]["key_env"]
            if provider in OPENAI_COMPATIBLE_PROVIDERS
            else "JARVIS_GEMINI_API_KEY"
        )
        if os.getenv(key_name, "").strip():
            ready.append(provider)
        elif configured_choice == provider:
            logger.warning("%s is selected, but %s is not configured.", provider, key_name)
    return ready


def get_cloud_model(provider):
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        config = OPENAI_COMPATIBLE_PROVIDERS[provider]
        return os.getenv(config["model_env"], config["default_model"]).strip() or config["default_model"]
    return os.getenv("JARVIS_GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL


def get_model_candidates():
    """Returns a prioritized model list with optional user override."""
    candidates = []
    env_model = os.getenv("JARVIS_MODEL")
    if env_model and env_model.strip():
        candidates.append(env_model.strip())
    candidates.extend(DEFAULT_MODELS)

    seen = set()
    ordered = []
    for model_name in candidates:
        if model_name and model_name not in seen:
            ordered.append(model_name)
            seen.add(model_name)
    return ordered


def _cache_key(provider, model_name, messages):
    cache_input = json.dumps(
        {
            "provider": provider,
            "model": model_name,
            "system_prompt": SYSTEM_PROMPT,
            "messages": messages,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(cache_input.encode("utf-8")).hexdigest()


def _open_cache_database():
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        cache_directory = os.path.join(local_app_data, "JarvisAssistant")
    else:
        cache_directory = os.path.join(os.path.expanduser("~"), ".cache", "jarvis_assistant")
    os.makedirs(cache_directory, exist_ok=True)
    database_path = os.path.join(cache_directory, "llm_responses.sqlite3")

    connection = sqlite3.connect(database_path, timeout=2)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS response_cache (
            cache_key TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            expires_at REAL NOT NULL,
            accessed_at REAL NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _get_cached_response(key):
    now = time.time()
    try:
        with closing(_open_cache_database()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT response, expires_at FROM response_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                response, expires_at = row
                if expires_at <= now:
                    connection.execute("DELETE FROM response_cache WHERE cache_key = ?", (key,))
                    return None
                connection.execute(
                    "UPDATE response_cache SET accessed_at = ? WHERE cache_key = ?",
                    (now, key),
                )
                return response
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not read the Jarvis response cache: %s", exc)
        return None


def _cache_response(key, response):
    now = time.time()
    try:
        with closing(_open_cache_database()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO response_cache
                        (cache_key, response, expires_at, accessed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, response, now + CACHE_TTL_SECONDS, now),
                )
                connection.execute("DELETE FROM response_cache WHERE expires_at <= ?", (now,))
                connection.execute(
                    """
                    DELETE FROM response_cache
                    WHERE cache_key IN (
                        SELECT cache_key FROM response_cache
                        ORDER BY accessed_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (CACHE_MAX_ENTRIES,),
                )
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not write the Jarvis response cache: %s", exc)


def _record_response(user_query, content):
    CONVERSATION_HISTORY.extend([
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": content},
    ])
    del CONVERSATION_HISTORY[:-MAX_HISTORY_MESSAGES]


def _request_gemini(messages, system_prompt=None, max_tokens=None, json_mode=False, model=None):
    api_key = os.getenv("JARVIS_GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("JARVIS_GEMINI_API_KEY is not configured.")

    model_name = model or os.getenv("JARVIS_GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    if not model_name:
        model_name = DEFAULT_GEMINI_MODEL

    contents = []
    for message in messages:
        contents.append({
            "role": "model" if message["role"] == "assistant" else "user",
            "parts": [{"text": message["content"]}],
        })

    generation_config = {
        "maxOutputTokens": max_tokens or CHAT_MAX_TOKENS,
        "temperature": 0.7,
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    thinking_level = os.getenv("JARVIS_GEMINI_THINKING_LEVEL", "").strip()
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt or SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": generation_config,
    }
    model_path = urllib.parse.quote(model_name, safe=".-_")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace").strip()
        if error_body:
            raise RuntimeError(
                f"Gemini API returned HTTP {exc.code}: {error_body[:400]}"
            ) from exc
        raise RuntimeError(f"Gemini API returned HTTP {exc.code}.") from exc

    candidates = response_data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no response candidates.")
    parts = candidates[0].get("content", {}).get("parts", [])
    content = "".join(part.get("text", "") for part in parts).strip()
    if not content:
        finish_reason = candidates[0].get("finishReason", "unknown")
        raise RuntimeError(
            f"Gemini returned an empty response (finishReason={finish_reason}). "
            "If this says MAX_TOKENS, raise JARVIS_MAX_TOKENS."
        )
    return model_name, content


def _request_openai_compatible(provider, messages, system_prompt=None, max_tokens=None, json_mode=False):
    config = OPENAI_COMPATIBLE_PROVIDERS[provider]
    model_name = get_cloud_model(provider)
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            *messages,
        ],
        "max_tokens": max_tokens or CHAT_MAX_TOKENS,
        "temperature": 0.7,
    }
    request = urllib.request.Request(
        config["endpoint"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ[config['key_env']].strip()}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace").strip()
        if error_body:
            raise RuntimeError(
                f"{provider} API returned HTTP {exc.code}: {error_body[:400]}"
            ) from exc
        raise RuntimeError(f"{provider} API returned HTTP {exc.code}.") from exc

    choices = response_data.get("choices", [])
    if not choices:
        raise RuntimeError(f"{provider} returned no response choices.")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )
    content = content.strip() if isinstance(content, str) else ""
    if not content:
        raise RuntimeError(f"{provider} returned an empty response.")
    return model_name, content


def _request_ollama(messages, system_prompt=None, max_tokens=None, json_mode=False):
    url = "http://localhost:11434/api/chat"

    if not is_ollama_available():
        return None, "I cannot reach the local language model, Sarthak. Please check that Ollama is running."

    ollama_messages = [
        {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
        *messages,
    ]

    for model_name in get_model_candidates():
        payload = {
            "model": model_name,
            "keep_alive": "10m",
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens or CHAT_MAX_TOKENS,
                "num_ctx": OLLAMA_CONTEXT_TOKENS,  # the agent's tool list needs more than the 2048 default
                "temperature": 0.7,
            },
        }
        if json_mode:
            payload["format"] = "json"

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                message_obj = res_data.get("message", {})
                content = message_obj.get("content", "").strip()
                if content:
                    return model_name, content
        except urllib.error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="ignore")
            error_text = response_text.lower()
            if any(marker in error_text for marker in [
                "not found",
                "not available",
                "llama-server process has terminated",
                "runner process has terminated",
                "exit status 0xc0000409",
            ]):
                continue
            return None, f"Mainframe background computation hitch: {response_text[:200]}"
        except urllib.error.URLError as exc:
            error_msg = str(exc)
            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                continue
            return None, "I cannot reach the local language model, Sarthak. Please check that Ollama is running."
        except Exception as exc:
            msg = str(exc).lower()
            if "timed out" in msg or "timeout" in msg:
                continue
            return None, f"Mainframe background computation hitch: {str(exc)}"

    return None, "The local language model is unavailable, Sarthak. Please check the Ollama logs or restart Ollama."


def brain(user_query):
    """Uses a configured online provider when reachable, otherwise local Ollama."""
    messages = [
        *CONVERSATION_HISTORY[-MAX_HISTORY_MESSAGES:],
        {"role": "user", "content": user_query},
    ]

    for provider in get_cloud_provider_candidates():
        if not is_internet_available(provider):
            continue
        try:
            model_name = get_cloud_model(provider)
            key = _cache_key(provider, model_name, messages)
            content = _get_cached_response(key)
            if content is None:
                if provider == "gemini":
                    model_name, content = _request_gemini(messages)
                else:
                    model_name, content = _request_openai_compatible(provider, messages)
                key = _cache_key(provider, model_name, messages)
                _cache_response(key, content)
            print(f"[LLM] Online mode: answered by {provider} ({model_name})")
            _record_response(user_query, content)
            return content
        except Exception as exc:
            logger.warning("%s request failed; trying the next available provider: %s", provider, exc)

    _announce_offline()
    for model_name in get_model_candidates():
        key = _cache_key("ollama", model_name, messages)
        content = _get_cached_response(key)
        if content is not None:
            _record_response(user_query, content)
            return content

    model_name, content = _request_ollama(messages)
    if model_name:
        key = _cache_key("ollama", model_name, messages)
        _cache_response(key, content)
        _record_response(user_query, content)
    return content


LOCAL_MODEL_FAILURE_PREFIXES = (
    "I cannot reach the local language model",
    "The local language model is unavailable",
    "Mainframe background computation hitch:",
)


def _announce_offline():
    if get_cloud_provider_candidates():
        print("[LLM] Offline mode: cloud unreachable, using local Ollama")
    else:
        print("[LLM] Offline mode: no cloud API key configured, using local Ollama")


def _with_retry(request, provider, others_available=False):
    """Retries once when a cloud provider is briefly overloaded (HTTP 503).

    If another cloud provider is configured, skip the retry and move on at once:
    a second try at a busy server is usually just as slow.
    """
    try:
        return request()
    except RuntimeError as exc:
        if "HTTP 503" not in str(exc) and "HTTP 429" not in str(exc):
            raise
        if others_available:
            print(f"[LLM] {provider} is busy; switching provider...")
            raise
        print(f"[LLM] {provider} is busy; retrying once...")
        time.sleep(0.5)
        return request()


def _gemini_with_backup(messages, system_prompt, max_tokens, json_mode, others):
    """When the main Gemini model is overloaded, ask a second Gemini model straight away instead of
    waiting to retry the busy one (JARVIS_GEMINI_BACKUP_MODEL, empty to turn off)."""
    backup = os.getenv("JARVIS_GEMINI_BACKUP_MODEL", "gemini-flash-latest").strip()
    try:
        return _request_gemini(messages, system_prompt, max_tokens, json_mode)
    except RuntimeError as exc:
        busy = "HTTP 503" in str(exc) or "HTTP 429" in str(exc)
        if not busy or not backup:
            if busy:
                return _with_retry(lambda: _request_gemini(messages, system_prompt, max_tokens, json_mode),
                                   "gemini", others)
            raise
    print(f"[LLM] gemini is busy; asking {backup} instead...")
    try:
        return _request_gemini(messages, system_prompt, max_tokens, json_mode, model=backup)
    except RuntimeError as exc:
        if others:
            raise
        print(f"[LLM] {backup} didn't answer either ({str(exc)[:80]}); retrying the main model once...")
        time.sleep(0.5)
        return _request_gemini(messages, system_prompt, max_tokens, json_mode)


def complete(messages, system_prompt, max_tokens=None, json_mode=False):
    started = time.time()
    try:
        return _complete(messages, system_prompt, max_tokens, json_mode)
    finally:
        print(f"[Timing] language model {time.time() - started:.1f}s")


def _complete(messages, system_prompt, max_tokens=None, json_mode=False):
    """Sends messages to the first working provider (cloud first, then Ollama).

    Returns (content, ok). When ok is False, content is a user-facing error message.
    No caching and no conversation-history update: callers decide what to remember.
    """
    candidates = get_cloud_provider_candidates()
    for index, provider in enumerate(candidates):
        if not is_internet_available(provider):
            continue
        others = index < len(candidates) - 1
        try:
            if provider == "gemini":
                model_name, content = _gemini_with_backup(messages, system_prompt, max_tokens, json_mode, others)
            else:
                model_name, content = _with_retry(
                    lambda: _request_openai_compatible(provider, messages, system_prompt, max_tokens, json_mode),
                    provider, others)
            print(f"[LLM] Online mode: answered by {provider} ({model_name})")
            return content, True
        except Exception as exc:
            if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)) and not isinstance(exc, urllib.error.HTTPError):
                mark_unreachable(provider)
            logger.warning("%s request failed; trying the next available provider: %s", provider, exc)

    _announce_offline()
    model_name, content = _request_ollama(messages, system_prompt, max_tokens, json_mode)
    if model_name:
        print(f"[LLM] Answered by local Ollama ({model_name})")
    return content, model_name is not None


def generate_text(instruction, max_tokens=None):
    """One-off long-form writing (essays, email bodies) without touching chat history."""
    writer_prompt = (
        "You are a skilled writer working for the user's desktop assistant. Follow the "
        "instruction exactly. Return only the requested text: no preface, no markdown "
        "fences, no notes about what you wrote."
    )
    content, ok = complete(
        [{"role": "user", "content": instruction}],
        writer_prompt,
        max_tokens=max_tokens or LONG_TEXT_MAX_TOKENS,
    )
    if not ok:
        raise RuntimeError(content)
    return content.strip()


def remember_exchange(user_query, reply):
    """Adds an exchange handled outside brain() to the shared conversation history."""
    if user_query and reply:
        _record_response(user_query, reply)

