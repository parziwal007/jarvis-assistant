import io
import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError

from brain import llm


class TemporaryCacheTestCase(unittest.TestCase):
    def setUp(self):
        self.cache_directory = tempfile.TemporaryDirectory()
        self.environment_patcher = patch.dict(
            os.environ,
            {"LOCALAPPDATA": self.cache_directory.name},
        )
        self.environment_patcher.start()

    def tearDown(self):
        self.environment_patcher.stop()
        self.cache_directory.cleanup()


class BrainRoutingTests(TemporaryCacheTestCase):
    def setUp(self):
        super().setUp()
        llm.CONVERSATION_HISTORY.clear()

    def tearDown(self):
        llm.CONVERSATION_HISTORY.clear()
        super().tearDown()

    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "test-key"})
    @patch.object(llm, "is_internet_available", return_value=True)
    @patch.object(llm, "_request_gemini", return_value=("gemini-3.5-flash-lite", "Gemini reply"))
    def test_uses_gemini_when_configured_and_reachable(self, request_gemini, _internet):
        with patch.object(llm, "_request_ollama") as request_ollama:
            response = llm.brain("Hello")

        self.assertEqual(response, "Gemini reply")
        request_gemini.assert_called_once()
        request_ollama.assert_not_called()
        self.assertEqual(llm.CONVERSATION_HISTORY[-1], {"role": "assistant", "content": "Gemini reply"})

    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "test-key"})
    @patch.object(llm, "is_internet_available", return_value=False)
    @patch.object(llm, "_request_gemini")
    @patch.object(llm, "_request_ollama", return_value=("qwen2.5:7b-instruct", "Local reply"))
    def test_uses_ollama_when_offline(self, request_ollama, request_gemini, _internet):
        response = llm.brain("Hello")

        self.assertEqual(response, "Local reply")
        request_ollama.assert_called_once()
        request_gemini.assert_not_called()

    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "test-key"})
    @patch.object(llm, "is_internet_available", return_value=True)
    @patch.object(llm, "_request_gemini", side_effect=OSError("Gemini unavailable"))
    @patch.object(llm, "_request_ollama", return_value=("qwen2.5:7b-instruct", "Local reply"))
    def test_falls_back_to_ollama_when_gemini_fails(self, request_ollama, _request_gemini, _internet):
        self.assertEqual(llm.brain("Hello"), "Local reply")
        request_ollama.assert_called_once()

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "XAI_API_KEY": "",
        "JARVIS_GEMINI_API_KEY": "",
        "JARVIS_CLOUD_PROVIDER": "auto",
    })
    @patch.object(llm, "is_internet_available", return_value=True)
    @patch.object(
        llm,
        "_request_openai_compatible",
        return_value=("x-ai/grok-4.7", "OpenRouter reply"),
    )
    def test_prefers_openrouter_when_its_key_is_configured(self, request_openrouter, _internet):
        with patch.object(llm, "_request_gemini") as request_gemini:
            response = llm.brain("Hello")

        self.assertEqual(response, "OpenRouter reply")
        request_openrouter.assert_called_once_with(
            "openrouter",
            [{"role": "user", "content": "Hello"}],
        )
        request_gemini.assert_not_called()

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "XAI_API_KEY": "test-xai-key",
        "JARVIS_GEMINI_API_KEY": "test-gemini-key",
        "JARVIS_CLOUD_PROVIDER": "auto",
    })
    def test_auto_provider_order_is_openrouter_then_xai_then_gemini(self):
        self.assertEqual(
            llm.get_cloud_provider_candidates(),
            ["openrouter", "xai", "gemini"],
        )

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "XAI_API_KEY": "test-xai-key",
        "JARVIS_GEMINI_API_KEY": "test-gemini-key",
        "JARVIS_CLOUD_PROVIDER": "xai",
    })
    def test_explicit_provider_selection_uses_only_the_selected_provider(self):
        self.assertEqual(llm.get_cloud_provider_candidates(), ["xai"])

    @patch.dict(os.environ, {
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "XAI_API_KEY": "test-xai-key",
        "JARVIS_GEMINI_API_KEY": "",
        "JARVIS_CLOUD_PROVIDER": "auto",
    })
    @patch.object(llm, "is_internet_available", return_value=True)
    @patch.object(
        llm,
        "_request_openai_compatible",
        side_effect=[RuntimeError("OpenRouter unavailable"), ("grok-4.7", "xAI reply")],
    )
    def test_tries_next_configured_provider_after_cloud_failure(self, request_provider, _internet):
        self.assertEqual(llm.brain("Hello"), "xAI reply")
        self.assertEqual(
            [call.args[0] for call in request_provider.call_args_list],
            ["openrouter", "xai"],
        )

    @patch.object(llm, "is_internet_available", return_value=False)
    @patch.object(llm, "_request_ollama", return_value=(None, "Ollama is unavailable"))
    def test_returns_local_unavailable_message_without_caching(self, _request_ollama, _internet):
        response = llm.brain("Hello")

        self.assertEqual(response, "Ollama is unavailable")
        self.assertEqual(llm.CONVERSATION_HISTORY, [])
        with closing(llm._open_cache_database()) as connection:
            count = connection.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0]
        self.assertEqual(count, 0)


class ResponseCacheTests(TemporaryCacheTestCase):
    def setUp(self):
        super().setUp()

    def test_persists_response_until_ttl_expires(self):
        with patch.object(llm.time, "time", return_value=100):
            llm._cache_response("prompt-key", "Cached reply")
        with patch.object(llm.time, "time", return_value=101):
            self.assertEqual(llm._get_cached_response("prompt-key"), "Cached reply")
        with patch.object(llm.time, "time", return_value=100 + llm.CACHE_TTL_SECONDS + 1):
            self.assertIsNone(llm._get_cached_response("prompt-key"))


class GeminiErrorTests(unittest.TestCase):
    @patch.dict(os.environ, {"JARVIS_GEMINI_API_KEY": "test-key"})
    @patch.object(
        llm.urllib.request,
        "urlopen",
        side_effect=HTTPError(
            "https://generativelanguage.googleapis.com",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":{"message":"API key not valid"}}'),
        ),
    )
    def test_includes_google_error_details_without_logging_request_headers(self, _urlopen):
        with self.assertRaisesRegex(RuntimeError, "API key not valid"):
            llm._request_gemini([{"role": "user", "content": "Hello"}])


class OpenAICompatibleRequestTests(unittest.TestCase):
    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-openrouter-key"})
    @patch.object(llm.urllib.request, "urlopen")
    def test_openrouter_request_uses_configured_grok_model_and_bearer_auth(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "Grok reply"}}],
        }).encode("utf-8")

        model_name, content = llm._request_openai_compatible(
            "openrouter",
            [{"role": "user", "content": "Hello"}],
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-openrouter-key")
        self.assertEqual(model_name, "x-ai/grok-4.7")
        self.assertEqual(payload["model"], "x-ai/grok-4.7")
        self.assertEqual(payload["max_tokens"], llm.CHAT_MAX_TOKENS)
        self.assertEqual(content, "Grok reply")

    @patch.dict(os.environ, {
        "XAI_API_KEY": "test-xai-key",
        "JARVIS_XAI_MODEL": "grok-test-model",
    })
    @patch.object(llm.urllib.request, "urlopen")
    def test_xai_request_uses_model_override_and_parses_multimodal_content(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": [{"type": "text", "text": "Direct xAI reply"}],
                },
            }],
        }).encode("utf-8")

        model_name, content = llm._request_openai_compatible(
            "xai",
            [{"role": "user", "content": "Hello"}],
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-xai-key")
        self.assertEqual(model_name, "grok-test-model")
        self.assertEqual(payload["model"], "grok-test-model")
        self.assertEqual(content, "Direct xAI reply")


if __name__ == "__main__":
    unittest.main()
