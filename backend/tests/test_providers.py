import asyncio
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI, NotFoundError

from agents.runtime import provider_failure
from providers.client import ModelSettings, request_options, create_client, assistant_message
from services.agent_service import run_recommendation_agent
from services.llm_service import generate_detail_ui
from test_agent_service import _profile, _candidate


class ProviderTests(unittest.TestCase):
    def test_legacy_qwen_config(self):
        with patch.dict(os.environ, {"QWEN_API_KEY": "test-secret", "QWEN_MODEL": "legacy-model"}, clear=True):
            s = ModelSettings.from_env()
            self.assertEqual(s.provider, "qwen")
            self.assertEqual(s.model, "legacy-model")
            self.assertNotIn("test-secret", repr(s))

    def test_provider_keys_are_isolated_and_generic_override(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "deepseek", "QWEN_API_KEY": "wrong"}, clear=True):
            s = ModelSettings.from_env()
            self.assertEqual(s.api_key, "")
            with self.assertRaises(ValueError):
                create_client(s)
            with patch.dict(os.environ, {"LLM_API_KEY": "test", "LLM_MODEL": "chosen"}):
                self.assertEqual(ModelSettings.from_env().model, "chosen")

    def test_invalid_config_rejected(self):
        for env in ({"LLM_PROVIDER": "typo"}, {"LLM_THINKING": "yes"}, {"LLM_TIMEOUT_SECONDS": "0"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                ModelSettings.from_env()

    def test_parameters_do_not_leak_between_providers(self):
        for provider in ("qwen", "deepseek", "ollama"):
            for thinking in (False, True):
                s = ModelSettings(provider, "model", "https://example.invalid", "test", thinking)
                opts = request_options(s, temperature=.15, max_tokens=100)
                self.assertEqual("temperature" in opts, not thinking)
                self.assertEqual("enable_search" in opts["extra_body"], provider == "qwen")
                self.assertEqual("thinking" in opts["extra_body"], provider == "deepseek")
                self.assertEqual("reasoning_effort" in opts["extra_body"], provider == "ollama")

    def test_local_ollama_uses_a_dummy_key_and_bypasses_proxy(self):
        async def run():
            with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_API_KEY": "remote-key"}, clear=True), \
                 patch("providers.client.DefaultAsyncHttpxClient", wraps=httpx.AsyncClient) as http_client:
                settings = ModelSettings.from_env()
                self.assertEqual(settings.model, "qwen3:4b")
                async with create_client(settings) as client:
                    self.assertEqual(client.api_key, "ollama")
                http_client.assert_called_once_with(trust_env=False)
        asyncio.run(run())

    def test_remote_ollama_requires_its_own_key(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "https://example.invalid/v1",
                                     "QWEN_API_KEY": "qwen-only"}, clear=True):
            with self.assertRaises(ValueError):
                create_client()

    def test_missing_model_failure_uses_public_guidance(self):
        response = httpx.Response(404, request=httpx.Request("POST", "http://localhost/v1/chat/completions"))
        code, message = provider_failure(NotFoundError("upstream-private", response=response, body=None))
        self.assertEqual(code, "model_not_found")
        self.assertIn("检查模型名称和安装状态", message)
        self.assertNotIn("upstream-private", message)

    def test_real_sdk_tool_history_and_reasoning_round_trip(self):
        async def run(provider):
            requests = []
            def handler(request):
                payload = json.loads(request.content)
                requests.append(payload)
                n = len(requests)
                # 包括无工具响应：该轮 reasoning 也必须回传。
                calls = None if n == 1 else [{"id": f"call{n}", "type": "function", "function": {
                    "name": "get_profile" if n == 2 else "ask_user",
                    "arguments": '{}' if n == 2 else '{"question":"补充城市？"}'}}]
                return httpx.Response(200, json={"id": "fake", "object": "chat.completion", "created": 1,
                    "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "", "reasoning_content": "private-test",
                                "tool_calls": calls}}]})
            client = AsyncOpenAI(api_key="test", base_url="https://example.invalid/v1",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            with patch.dict(os.environ, {"LLM_PROVIDER": provider, "LLM_THINKING": "true"}, clear=True), \
                 patch("services.agent_service._async_client", return_value=client), \
                 patch("services.agent_service.run_rule_engine", return_value=[_candidate(1)]):
                events = [e async for e in run_recommendation_agent(_profile())]
            self.assertEqual(events[-1]["event"], "waiting")
            self.assertEqual(requests[1]["messages"][-2]["reasoning_content"], "private-test")
            self.assertEqual(requests[2]["messages"][-2]["reasoning_content"], "private-test")
            self.assertEqual(requests[2]["messages"][-1]["tool_call_id"], "call2")
            self.assertNotIn("private-test", json.dumps(events))
            self.assertTrue(client.is_closed())
        for provider in ("qwen", "deepseek"):
            asyncio.run(run(provider))

    def test_tool_message_serialization(self):
        message = SimpleNamespace(content="", reasoning_content="keep", tool_calls=[
            SimpleNamespace(id="1", function=SimpleNamespace(name="get_profile", arguments="{}"))])
        result = assistant_message(message)
        self.assertEqual(result["reasoning_content"], "keep")
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "get_profile")

    def test_detail_uses_adapter_and_closes_stream_and_client(self):
        class Stream:
            closed = False
            def __aiter__(self):
                async def chunks():
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content='{"html":"ok","chartConfig":{}}'))])
                return chunks()
            async def close(self):
                self.closed = True
        class Client:
            closed = False
            def __init__(self):
                self.stream = Stream()
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            async def create(self, **kwargs):
                self.kwargs = kwargs
                return self.stream
            async def close(self):
                self.closed = True
        for provider in ("qwen", "deepseek"):
            client = Client()
            with patch.dict(os.environ, {"LLM_PROVIDER": provider}, clear=True), \
                 patch("providers.client.create_client", return_value=client), \
                 patch("services.llm_service._build_detail_user_prompt", return_value="test"):
                result = asyncio.run(generate_detail_ui({}, {}))
            self.assertEqual(result["html"], "ok")
            self.assertTrue(client.closed and client.stream.closed)
            self.assertEqual("thinking" in client.kwargs["extra_body"], provider == "deepseek")

    def test_detail_error_and_cancellation_close_client(self):
        class Client:
            closed = False
            def __init__(self, error):
                self.error = error
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            async def create(self, **kwargs):
                raise self.error
            async def close(self):
                self.closed = True
        for error, expected in ((RuntimeError("secret-provider-body"), ValueError),
                                (asyncio.CancelledError(), asyncio.CancelledError)):
            client = Client(error)
            with patch.dict(os.environ, {}, clear=True), \
                 patch("providers.client.create_client", return_value=client), \
                 patch("services.llm_service._build_detail_user_prompt", return_value="test"):
                with self.assertRaises(expected) as caught:
                    asyncio.run(generate_detail_ui({}, {}))
            self.assertTrue(client.closed)
            self.assertNotIn("secret-provider-body", str(caught.exception))
