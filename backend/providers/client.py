"""供应商边界：配置、参数、消息回传。业务层不拼接供应商参数。"""
from dataclasses import dataclass, field
import os
from typing import Literal
from urllib.parse import urlsplit

from openai import AsyncOpenAI, DefaultAsyncHttpxClient


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ModelConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSettings:
    provider: Literal["qwen", "deepseek", "ollama"]
    model: str
    base_url: str
    api_key: str = field(repr=False)
    thinking: bool = False
    timeout: float = 60.0

    @classmethod
    def from_env(cls):
        provider = os.getenv("LLM_PROVIDER", "qwen").strip().lower()
        if provider not in {"qwen", "deepseek", "ollama"}:
            raise ValueError("LLM_PROVIDER 必须为 qwen、deepseek 或 ollama")
        prefix = provider.upper()
        defaults = {
            "qwen": ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "deepseek": ("deepseek-v4-pro", "https://api.deepseek.com"),
            "ollama": ("qwen3:4b", "http://127.0.0.1:11434/v1"),
        }
        model, url = defaults[provider]
        thinking = os.getenv("LLM_THINKING", "false").lower()
        if thinking not in {"true", "false"}:
            raise ValueError("LLM_THINKING 必须为 true 或 false")
        timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
        if not 0 < timeout <= 300:
            raise ValueError("LLM_TIMEOUT_SECONDS 必须在 (0, 300] 内")
        return cls(provider=provider,
                   model=os.getenv("LLM_MODEL") or os.getenv(prefix + "_MODEL") or model,
                   base_url=os.getenv("LLM_BASE_URL") or os.getenv(prefix + "_BASE_URL") or url,
                   api_key=os.getenv("LLM_API_KEY") or os.getenv(prefix + "_API_KEY") or "",
                   thinking=thinking == "true", timeout=timeout)


def create_client(settings: ModelSettings | None = None):
    settings = settings or ModelSettings.from_env()
    hostname = urlsplit(settings.base_url).hostname
    local_ollama = settings.provider == "ollama" and hostname in LOOPBACK_HOSTS
    api_key = "ollama" if local_ollama else settings.api_key
    if not api_key or api_key == "your_api_key_here":
        raise ModelConfigurationError(f"未配置 {settings.provider.upper()}_API_KEY 或 LLM_API_KEY")
    direct_hosts = {host.strip().lower() for host in os.getenv("LLM_NO_PROXY", "").split(",") if host.strip()}
    http_client = DefaultAsyncHttpxClient(trust_env=False) if local_ollama or hostname in direct_hosts else None
    return AsyncOpenAI(api_key=api_key, base_url=settings.base_url,
                       timeout=settings.timeout, max_retries=0, http_client=http_client)


def request_options(settings: ModelSettings, *, temperature: float, max_tokens: int):
    options = {"model": settings.model, "max_tokens": max_tokens}
    if not settings.thinking:
        options["temperature"] = temperature
    if settings.provider == "qwen":
        options["extra_body"] = {"enable_search": False, "enable_thinking": settings.thinking}
    elif settings.provider == "deepseek":
        options["extra_body"] = {"thinking": {"type": "enabled" if settings.thinking else "disabled"}}
    elif settings.provider == "ollama":
        options["extra_body"] = {"reasoning_effort": "low" if settings.thinking else "none"}
    else:
        raise ValueError("不支持的模型供应商")
    return options


def assistant_message(message):
    """保留 SDK 返回字段（包括 reasoning_content）；仅用于模型上下文，不发往 UI。"""
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    # 简单客户端与测试替身也遵守相同协议。
    result = {"role": "assistant", "content": getattr(message, "content", None) or ""}
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is not None:
        result["reasoning_content"] = reasoning
    calls = getattr(message, "tool_calls", None)
    if calls:
        result["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {
                "name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    return result
