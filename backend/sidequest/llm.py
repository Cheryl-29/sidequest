"""Model adapter. The provider appears in this file and nowhere else.

Everything a model returns is untrusted structured data. It may choose, phrase and
prioritise; it may never assert that a place is open, cheap or reachable -- the
executor re-derives every such claim from providers (plan v2 §4.1). So the schemas
below carry preferences and wording, never times, fares or feasibility.

The endpoint, model id and key all come from the environment, so pointing this at an
OpenAI-compatible gateway is a config change rather than a code change.
"""

import json
import time
from typing import Any, Protocol

import httpx

from .providers import load_local_env

DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_DECISION_BUDGET = 6  # plan v2 §4.5


class ModelError(RuntimeError):
    """A model call could not produce a usable decision."""


class ModelAuthError(ModelError):
    pass


class ModelRateLimitError(ModelError):
    pass


class ModelTimeoutError(ModelError):
    pass


class ModelSchemaError(ModelError):
    pass


class ModelBudgetExhausted(ModelError):
    pass


def schema(name: str, properties: dict[str, Any]) -> dict:
    """Strict JSON schema. Every action reports a summary (plan v2 §4.2)."""
    properties = {**properties, "summary": {"type": "string"}}
    return {
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


class Model(Protocol):
    calls: int
    prompt_tokens: int
    completion_tokens: int

    def decide(self, name: str, system: str, user: str, spec: dict) -> dict: ...


class OpenAIModel:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        limit: int = DEFAULT_DECISION_BUDGET,
        timeout: float = 8.0,
        client: httpx.Client | None = None,
    ):
        key = api_key or load_local_env("OPENAI_API_KEY") or ""
        if not key or any(c.isspace() for c in key):
            raise ModelAuthError("OPENAI_API_KEY 未配置或格式无效")
        self.api_key = key
        self.model = model or load_local_env("OPENAI_MODEL") or DEFAULT_MODEL
        self.endpoint = endpoint or load_local_env("OPENAI_BASE_URL") or DEFAULT_ENDPOINT
        self.limit = limit
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def consume(self) -> None:
        if self.calls >= self.limit:
            raise ModelBudgetExhausted(f"模型决策预算已耗尽（{self.limit} 次）")
        self.calls += 1

    def decide(self, name: str, system: str, user: str, spec: dict) -> dict:
        self.consume()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_schema", "json_schema": spec},
            "temperature": 0.7,
        }
        started = time.perf_counter()
        try:
            response = self.client.post(
                self.endpoint,
                json=payload,
                # Key rides in the header only: never a query param, never logged.
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(f"模型请求超时（{name}）") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"模型来源暂时不可用（{name}）") from exc
        if response.status_code in (401, 403):
            raise ModelAuthError("模型接口拒绝了当前 API key")
        if response.status_code == 429:
            raise ModelRateLimitError("模型请求频率或额度受限")
        if not response.is_success:
            raise ModelError(f"模型返回 HTTP {response.status_code}")
        try:
            body = response.json()
            usage = body.get("usage") or {}
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelSchemaError(f"模型响应结构无法解析（{name}）") from exc
        if not isinstance(data, dict) or "summary" not in data:
            raise ModelSchemaError(f"模型响应缺少 summary（{name}）")
        data["_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return data


def openai_configured() -> bool:
    """Whether the agent path can run. Never exposes the key itself."""
    key = load_local_env("OPENAI_API_KEY") or ""
    return bool(key and not any(c.isspace() for c in key))
