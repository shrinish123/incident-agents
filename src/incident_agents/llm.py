"""LLM gateway: the single path from agents to a model.

It picks the model and effort for each role, records tokens, latency and an
estimated cost per call, enforces an investigation-wide budget, and turns API
failures into one exception type the agent runtime knows how to recover from.

Two clients implement the interface: `OpenAICompatLLM` (chat completions over
HTTP, for Groq, NVIDIA, Cerebras, a local Ollama server or any other
OpenAI-compatible endpoint) and `AnthropicLLM` (the official SDK). `make_llm`
picks one from the environment: IA_PROVIDER, else whichever API key is set.

`ScriptedLLM` implements the same interface without a network call, so the
orchestration logic is tested deterministically.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import anthropic
import httpx

# USD per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    # Groq list prices. The cached-input price is not published per model; half the input price is assumed.
    "openai/gpt-oss-120b": (0.15, 0.60, 0.075),
    "openai/gpt-oss-20b": (0.075, 0.30, 0.0375),
}
# OpenAI-compatible endpoints. `tokens_param` is the name each one uses for the output cap.
PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "strong": "openai/gpt-oss-120b", "fast": "openai/gpt-oss-120b", "tokens_param": "max_completion_tokens"},
    # The free endpoint stalls when requests overlap, so calls go one at a time with a short timeout.
    "nvidia": {"name": "NVIDIA", "base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY",
               "strong": "nvidia/nemotron-3-ultra-550b-a55b", "fast": "minimaxai/minimax-m3.1-flash", "tokens_param": "max_tokens",
               "concurrency": "1", "timeout_s": "120"},
    "cerebras": {"name": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
                 "strong": "gpt-oss-120b", "fast": "gpt-oss-120b", "tokens_param": "max_completion_tokens"},
    # A model on this machine: no key, no rate limit, no cost. Start the server with a large context window.
    "ollama": {"name": "Ollama", "base_url": "http://localhost:11434/v1", "key_env": "",
               "strong": "gpt-oss:20b", "fast": "gpt-oss:20b", "tokens_param": "max_tokens", "concurrency": "1", "timeout_s": "1800"},
    # Anything else that speaks chat completions: set IA_BASE_URL, IA_API_KEY, IA_MODEL_STRONG and IA_MODEL_FAST.
    "openai_compatible": {"name": "the model", "base_url": "", "key_env": "IA_API_KEY", "strong": "", "fast": "", "tokens_param": "max_tokens"},
}
DEFAULT_MODELS = {"anthropic": ("claude-opus-5-5", "claude-sonnet-5-5"), **{k: (v["strong"], v["fast"]) for k, v in PROVIDERS.items()}}


def detect_provider() -> str:
    """IA_PROVIDER if set, else the first provider whose key is present."""
    chosen = os.environ.get("IA_PROVIDER", "").strip().lower()
    if chosen in DEFAULT_MODELS:
        return chosen
    for name in ("groq", "nvidia", "cerebras"):
        if os.environ.get(PROVIDERS[name]["key_env"]):
            return name
    return "anthropic"


@dataclass(frozen=True)
class RoleConfig:
    model: str
    effort: str
    max_tokens: int = 16000


def default_roles(provider: str | None = None) -> dict[str, RoleConfig]:
    """Model per role. Override with IA_MODEL_STRONG / IA_MODEL_FAST, and the output cap with IA_MAX_OUTPUT_TOKENS."""
    provider = provider or detect_provider()
    strong = os.environ.get("IA_MODEL_STRONG") or DEFAULT_MODELS[provider][0]
    fast = os.environ.get("IA_MODEL_FAST") or DEFAULT_MODELS[provider][1]
    cap = int(os.environ.get("IA_MAX_OUTPUT_TOKENS") or (16000 if provider == "anthropic" else 8000))
    return {
        "reasoning": RoleConfig(strong, "high", cap),
        "critic": RoleConfig(strong, "high", cap),
        "baseline": RoleConfig(strong, "high", cap),
        "specialist": RoleConfig(fast, "medium", cap),
        "report": RoleConfig(fast, "medium", cap),
        "judge": RoleConfig(fast, "low", min(cap, 4000)),
    }


def _price(model: str) -> tuple[float, float, float]:
    """Price for a model id; a dated id ("claude-sonnet-5-5-20261001") uses its family's price. Unknown models are priced high."""
    if model in PRICES:
        return PRICES[model]
    known = [name for name in PRICES if model.startswith(name)]
    return PRICES[max(known, key=len)] if known else PRICES["claude-opus-5-5"]


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self, model: str) -> float:
        inp, out, cached = _price(model)
        return (
            self.input_tokens * inp + self.output_tokens * out + self.cache_read_tokens * cached + self.cache_write_tokens * inp * 1.25
        ) / 1_000_000


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    content: Any  # assistant content to append to the conversation unchanged
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    latency_ms: int = 0


@dataclass
class LLMRequest:
    agent: str
    role: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    schema: dict[str, Any] | None = None


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class BudgetExceeded(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass
class CallRecord:
    agent: str
    role: str
    model: str
    usage: Usage
    latency_ms: int
    stop_reason: str


class UsageLedger:
    """Every model call of one investigation, with a hard ceiling."""

    def __init__(self, max_calls: int = 120, max_tokens: int = 3_000_000, max_cost_usd: float | None = None) -> None:
        self.max_calls, self.max_tokens = max_calls, max_tokens
        # A ceiling in money, since tokens differ in price by model and by whether they were cached. IA_MAX_COST_USD sets it per investigation.
        self.max_cost_usd = max_cost_usd if max_cost_usd is not None else (float(os.environ.get("IA_MAX_COST_USD") or 0) or None)
        self.calls: list[CallRecord] = []

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def tokens(self) -> int:
        return sum(c.usage.total for c in self.calls)

    def remaining(self) -> dict[str, int]:
        return {"calls": self.max_calls - len(self.calls), "tokens": self.max_tokens - self.tokens}

    @property
    def cost_usd(self) -> float:
        return sum(c.usage.cost(c.model) for c in self.calls)

    def exhausted(self) -> bool:
        left = self.remaining()
        return left["calls"] <= 0 or left["tokens"] <= 0 or (self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd)

    def check(self) -> None:
        if self.exhausted():
            raise BudgetExceeded(f"LLM budget used up ({len(self.calls)} calls, {self.tokens} tokens, ${self.cost_usd:.2f})")

    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.calls),
            "input_tokens": sum(c.usage.input_tokens + c.usage.cache_read_tokens + c.usage.cache_write_tokens for c in self.calls),
            "output_tokens": sum(c.usage.output_tokens for c in self.calls),
            "cache_read_tokens": sum(c.usage.cache_read_tokens for c in self.calls),
            "total_tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 4),
            "llm_latency_ms": sum(c.latency_ms for c in self.calls),
        }

    def by_agent(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            row = out.setdefault(c.agent, {"calls": 0, "tokens": 0, "cost_usd": 0.0, "model": c.model})
            row["calls"] += 1
            row["tokens"] += c.usage.total
            row["cost_usd"] = round(row["cost_usd"] + c.usage.cost(c.model), 4)
        return out


class LLM(Protocol):
    ledger: UsageLedger

    async def complete(self, req: LLMRequest) -> LLMResponse: ...


class AnthropicLLM:
    """Claude through the official SDK. Credentials come from the environment."""

    def __init__(
        self,
        ledger: UsageLedger | None = None,
        roles: dict[str, RoleConfig] | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        fallbacks: bool | None = None,
    ) -> None:
        self.ledger = ledger or UsageLedger()
        self.roles = roles or default_roles()
        # One request in the first live run hung for the full ten-minute default and only then was retried.
        # No call in an investigation legitimately takes that long, so give up sooner and retry.
        self._client = client or anthropic.AsyncAnthropic(max_retries=2, timeout=float(os.environ.get("IA_LLM_TIMEOUT_S") or 240))
        # Above this size a schema goes in the instructions rather than being compiled for constrained output.
        self._grammar_max = int(os.environ.get("IA_SCHEMA_GRAMMAR_MAX_CHARS") or 5000)
        # Server-side fallback: if a safety classifier declines a request, the API
        # re-runs it on a fallback model instead of failing the investigation.
        self._fallbacks = os.environ.get("IA_LLM_FALLBACKS", "1") != "0" if fallbacks is None else fallbacks
        # Schemas the API would not compile for constrained output ("grammar is too large"). They go in
        # the instructions instead, and the runtime's parse-and-repair round does the checking.
        self._in_prompt: set[str] = set()

    def _kwargs(self, req: LLMRequest, cfg: RoleConfig, schema_key: str) -> dict[str, Any]:
        system, output_config = req.system, {"effort": cfg.effort}
        if req.schema is not None and schema_key in self._in_prompt:
            system += ("\n\nYour final message must be exactly one JSON object, with no text or code fence around it, "
                       "that matches this JSON Schema:\n" + schema_key)
        elif req.schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.schema}
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": req.messages,
            "cache_control": {"type": "ephemeral"},  # caches the growing tool loop between turns
            "output_config": output_config,
        }
        if req.tools:
            kwargs["tools"] = req.tools
        return kwargs

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.ledger.check()
        cfg = self.roles[req.role]
        schema_key = json.dumps(req.schema, separators=(",", ":"), sort_keys=True) if req.schema is not None else ""
        if len(schema_key) > self._grammar_max:
            self._in_prompt.add(schema_key)
        started = time.monotonic()
        try:
            try:
                msg = await self._send(self._kwargs(req, cfg, schema_key))
            except anthropic.BadRequestError as exc:
                if not schema_key or schema_key in self._in_prompt or "grammar" not in str(exc.message).lower():
                    raise
                self._in_prompt.add(schema_key)
                msg = await self._send(self._kwargs(req, cfg, schema_key))
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMError(f"Model credentials were rejected ({exc.status_code})") from None
        except (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.UnprocessableEntityError) as exc:
            raise LLMError(f"Model request was invalid: {str(exc.message)[:300]}") from None
        except anthropic.RateLimitError:
            raise LLMError("Model rate limit hit after retries", retryable=True) from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Model API error {exc.status_code}", retryable=exc.status_code >= 500) from None
        except anthropic.APIConnectionError:
            raise LLMError("Model API unreachable", retryable=True) from None

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        latency = int((time.monotonic() - started) * 1000)
        self.ledger.record(CallRecord(req.agent, req.role, msg.model or cfg.model, usage, latency, msg.stop_reason or ""))
        if msg.stop_reason == "refusal":
            raise LLMError("The model declined the request")
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input) if isinstance(b.input, dict) else {}) for b in msg.content if b.type == "tool_use"]
        return LLMResponse(text, calls, msg.stop_reason or "", msg.content, usage, msg.model or cfg.model, latency)

    async def _send(self, kwargs: dict[str, Any]) -> Any:
        if not self._fallbacks:
            return await self._client.messages.create(**kwargs)
        try:
            return await self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.BadRequestError as exc:
            if "fallback" not in str(exc.message).lower():
                raise
            self._fallbacks = False  # not available for this account or model: carry on without it
            return await self._client.messages.create(**kwargs)


# --- OpenAI-compatible chat completions (Groq, NVIDIA, Cerebras, Ollama, ...) ----------

_ORG_RE = re.compile(r"org_[A-Za-z0-9]+")
_WAIT_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def _duration_s(text: str) -> float | None:
    """'1m26.4s', '4.2s' or '7' as seconds."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)(?:s)?)?", text.strip())
    if not m or not any(m.groups()):
        return None
    return int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0)


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The agent loop keeps conversations as content blocks (text, tool_use, tool_result); chat completions wants roles."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": msg["role"], "content": content})
            continue
        blocks = [b if isinstance(b, dict) else b.model_dump() for b in content]
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if msg["role"] == "assistant":
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
            continue
        for b in blocks:
            if b.get("type") == "tool_result":
                body = b.get("content")
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": body if isinstance(body, str) else json.dumps(body, default=str)})
        if text:
            out.append({"role": "user", "content": text})
    return out


class _Pacer:
    """Holds requests back when the account's tokens-per-minute window cannot fit them, instead of collecting 429s."""

    def __init__(self) -> None:
        self.remaining: float | None = None
        self.reset_at = 0.0
        self.lock = asyncio.Lock()

    async def before(self, estimate: int, sleep: Callable[[float], Any]) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.remaining is not None and now < self.reset_at and estimate > self.remaining:
                await sleep(self.reset_at - now + 0.2)
                self.remaining = None
            elif self.remaining is not None:
                self.remaining -= estimate

    def after(self, headers: Any) -> None:
        try:
            self.remaining = float(headers["x-ratelimit-remaining-tokens"])
            self.reset_at = time.monotonic() + (_duration_s(headers.get("x-ratelimit-reset-tokens", "")) or 0.0)
        except (KeyError, ValueError):
            pass


_PACERS: dict[str, _Pacer] = {}
_GATES: dict[tuple[str, int], asyncio.Semaphore] = {}


def _gate(provider: str, limit: int) -> asyncio.Semaphore:
    """One limiter per provider and event loop, shared by every client in the process."""
    key = (provider, id(asyncio.get_running_loop()))
    if key not in _GATES:
        _GATES[key] = asyncio.Semaphore(limit)
    return _GATES[key]


class OpenAICompatLLM:
    """Any provider that speaks the OpenAI chat-completions API: Groq, NVIDIA, Cerebras, a local Ollama server.

    Differences from the Anthropic path that matter here:
      - structured output cannot be combined with tools on these APIs, so an agent with tools is
        told the schema in its instructions and the runtime's parse-and-repair round does the
        checking; an agent without tools gets `response_format: json_schema`, strict first, then
        best effort, then (for servers without it) the schema in the instructions
      - rate limits are per minute and per day and can be small: requests are paced from the
        rate-limit headers, a 429 is waited out (up to IA_RATE_WAIT_S), and a request larger
        than the per-minute limit fails at once with a message that says so
    """

    def __init__(
        self,
        ledger: UsageLedger | None = None,
        roles: dict[str, RoleConfig] | None = None,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        max_wait_s: float | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        provider: str = "groq",
    ) -> None:
        self.provider, self._cfg = provider, PROVIDERS[provider]
        self.ledger = ledger or UsageLedger()
        self.roles = roles or default_roles(provider)
        self._key = api_key or (os.environ.get(self._cfg["key_env"], "") if self._cfg["key_env"] else "")
        base = os.environ.get("IA_BASE_URL") or self._cfg["base_url"]
        self._url = base.rstrip("/") + "/chat/completions"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(float(self._cfg.get("timeout_s", 300)), connect=15.0))
        # How many requests may be in flight at once (0 = no cap). Override with IA_LLM_CONCURRENCY.
        self._concurrency = int(os.environ.get("IA_LLM_CONCURRENCY") or self._cfg.get("concurrency", 0))
        self._max_wait = float(os.environ.get("IA_RATE_WAIT_S", 900)) if max_wait_s is None else max_wait_s
        self._sleep = sleep
        self._format = "strict"  # strict -> schema (best effort) -> prompt (no response_format), stepping down when the API refuses

    @property
    def _strict(self) -> bool:
        return self._format == "strict"

    def _body(self, req: LLMRequest, cfg: RoleConfig) -> dict[str, Any]:
        system = req.system
        body: dict[str, Any] = {"model": cfg.model, self._cfg["tokens_param"]: cfg.max_tokens}
        if "gpt-oss" in cfg.model:
            body["reasoning_effort"] = cfg.effort if cfg.effort in ("low", "medium", "high") else "medium"
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in req.tools]
            if req.schema is not None:
                system += ("\n\nWhen you have finished using tools, your final message must be exactly one JSON object, with no text around it, "
                           "that matches this JSON Schema:\n" + json.dumps(req.schema, separators=(",", ":")))
        elif req.schema is not None and self._format == "prompt":
            system += "\n\nYour reply must be exactly one JSON object, with no text around it, that matches this JSON Schema:\n" + json.dumps(req.schema, separators=(",", ":"))
        elif req.schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": req.schema, **({"strict": True} if self._strict else {})}}
        body["messages"] = to_openai_messages(system, req.messages)
        return body

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        if not self._concurrency:
            return await self._client.post(self._url, json=body, headers=headers)
        async with _gate(self.provider, self._concurrency):
            return await self._client.post(self._url, json=body, headers=headers)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.ledger.check()
        if not self._key and self._cfg["key_env"]:
            raise LLMError(f"{self._cfg['key_env']} is not set")
        cfg = self.roles[req.role]
        pacer = _PACERS.setdefault(cfg.model, _Pacer())
        started, waited, regenerated, server_errors = time.monotonic(), 0.0, 0, 0
        while True:
            body = self._body(req, cfg)
            await pacer.before(len(json.dumps(body)) // 4, self._sleep)
            try:
                resp = await self._post(body)
            except httpx.HTTPError:
                server_errors += 1
                if server_errors > 3:
                    raise LLMError(f"Model API unreachable ({self._url.split('/chat/')[0]})", retryable=True) from None
                await self._sleep(2.0 * server_errors)
                continue
            pacer.after(resp.headers)
            if resp.status_code == 200:
                break
            try:
                err = resp.json().get("error") or {}
            except ValueError:
                err = {}
            message, code = _ORG_RE.sub("org_...", str(err.get("message") or ""))[:400], str(err.get("code") or "")
            if resp.status_code in (401, 403):
                raise LLMError(f"Model credentials were rejected ({resp.status_code})")
            if resp.status_code == 413 or (resp.status_code == 429 and "request too large" in message.lower()):
                raise LLMError(f"The request is larger than this {self._cfg['name']} account's rate limit allows in one call: {message}")
            if resp.status_code == 429:
                wait = _duration_s(resp.headers.get("retry-after", "")) or next(
                    (int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + float(m[3] or 0) for m in [_WAIT_RE.search(message)] if m and any(m.groups())), 10.0)
                if waited + wait > self._max_wait:
                    raise LLMError(f"Model rate limit: the API asks to wait {wait:.0f}s ({message})", retryable=False)
                waited += wait
                await self._sleep(wait + random.uniform(0.2, 1.0))
                continue
            if resp.status_code == 400 and code in ("tool_use_failed", "json_validate_failed"):
                regenerated += 1  # the model produced a malformed tool call or answer: generate again
                if regenerated > 2:
                    raise LLMError(f"The model kept producing malformed output ({code})", retryable=True)
                continue
            if resp.status_code in (400, 422) and "response_format" in body:
                # This schema, or structured output itself, is not accepted here: step down and ask again.
                self._format = "schema" if self._format == "strict" else "prompt"
                continue
            if resp.status_code >= 500:
                server_errors += 1
                if server_errors > 3:
                    raise LLMError(f"Model API error {resp.status_code}", retryable=True)
                await self._sleep(2.0 * server_errors)
                continue
            raise LLMError(f"Model request was invalid ({resp.status_code}): {message}")

        data = resp.json()
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        calls = []
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(call.get("id") or f"call_{len(calls)}", str(fn.get("name") or ""), args if isinstance(args, dict) else {}))
        u = data.get("usage") or {}
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage = Usage(input_tokens=int(u.get("prompt_tokens") or 0) - cached, output_tokens=int(u.get("completion_tokens") or 0), cache_read_tokens=cached)
        stop = {"length": "max_tokens", "tool_calls": "tool_use", "stop": "end_turn"}.get(choice.get("finish_reason") or "", choice.get("finish_reason") or "")
        latency = int((time.monotonic() - started) * 1000)
        model = data.get("model") or cfg.model
        self.ledger.record(CallRecord(req.agent, req.role, model, usage, latency, stop))
        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        return LLMResponse(text, calls, stop, content, usage, model, latency)


GroqLLM = OpenAICompatLLM  # the default provider of this client


def make_llm(ledger: UsageLedger | None = None, provider: str | None = None) -> LLM:
    """The model client for the configured provider."""
    provider = provider or detect_provider()
    if provider in PROVIDERS:
        return OpenAICompatLLM(ledger, provider=provider)
    return AnthropicLLM(ledger, roles=default_roles("anthropic"))


def have_credentials() -> bool:
    provider = detect_provider()
    if provider in PROVIDERS:
        key_env = PROVIDERS[provider]["key_env"]
        return not key_env or bool(os.environ.get(key_env))
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class ScriptedLLM:
    """Test double. `handler(request)` returns a dict: {"text": ...} or {"tool_calls": [{"name", "input"}]}."""

    def __init__(self, handler: Callable[[LLMRequest], dict[str, Any]], ledger: UsageLedger | None = None) -> None:
        self.ledger = ledger or UsageLedger()
        self._handler = handler
        self.requests: list[LLMRequest] = []
        self._n = 0

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.ledger.check()
        req = replace(req, messages=list(req.messages))  # snapshot: the caller keeps appending to its list
        self.requests.append(req)
        out = self._handler(req)
        if isinstance(out, Exception):
            raise out
        calls = []
        for call in out.get("tool_calls", []):
            self._n += 1
            calls.append(ToolCall(f"toolu_{self._n}", call["name"], call.get("input", {})))
        text = out.get("text", "")
        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
        usage = Usage(input_tokens=out.get("input_tokens", 100), output_tokens=out.get("output_tokens", 20))
        stop = out.get("stop_reason") or ("tool_use" if calls else "end_turn")
        self.ledger.record(CallRecord(req.agent, req.role, "scripted", usage, 1, stop))
        return LLMResponse(text, calls, stop, content, usage, "scripted", 1)
