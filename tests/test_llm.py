import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from incident_agents.llm import AnthropicLLM, BudgetExceeded, LLMError, LLMRequest, UsageLedger


def message(content, stop="end_turn", model="claude-sonnet-5-5"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=4000, cache_creation_input_tokens=500)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model=model)


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def client(beta=(), plain=()):
    c = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(beta)), messages=FakeMessages(plain))
    return c


def api_error(cls, status, text):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"), json={"error": {"message": text}})
    return cls(text, response=resp, body={"error": {"message": text}})


REQ = LLMRequest(agent="code", role="specialist", system="sys", messages=[{"role": "user", "content": "hi"}],
                 tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}], schema={"type": "object"})


async def test_request_shape_usage_and_tool_calls():
    blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="ok"),
              SimpleNamespace(type="tool_use", id="tu_1", name="t", input={"a": 1})]
    c = client(beta=[message(blocks, "tool_use")])
    llm = AnthropicLLM(client=c, fallbacks=True)
    resp = await llm.complete(REQ)
    sent = c.beta.messages.calls[0]
    assert sent["model"] == "claude-sonnet-5-5" and sent["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"] and sent["tools"] == REQ.tools
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"} and "thinking" not in sent and "tool_choice" not in sent
    assert resp.text == "ok" and resp.tool_calls[0].name == "t" and resp.tool_calls[0].input == {"a": 1} and resp.content is blocks
    totals = llm.ledger.totals()
    assert totals["input_tokens"] == 5500 and totals["output_tokens"] == 200 and totals["total_tokens"] == 5700
    assert totals["cost_usd"] == round((1000 * 2 + 200 * 10 + 4000 * 0.1 + 500 * 2.5) / 1e6, 4)
    assert llm.ledger.by_agent()["code"]["calls"] == 1

    strong = client(plain=[message([SimpleNamespace(type="text", text="{}")], model="claude-opus-5-5")])
    await AnthropicLLM(client=strong, fallbacks=False).complete(LLMRequest("reasoning", "reasoning", "s", [{"role": "user", "content": "x"}]))
    sent = strong.messages.calls[0]
    assert sent["model"] == "claude-opus-5-5" and sent["output_config"] == {"effort": "high"} and "tools" not in sent


async def test_errors_refusals_and_budget_are_mapped():
    c = client(beta=[api_error(anthropic.BadRequestError, 400, "fallbacks: not supported")], plain=[message([SimpleNamespace(type="text", text="ok")])])
    llm = AnthropicLLM(client=c, fallbacks=True)
    assert (await llm.complete(REQ)).text == "ok" and "fallbacks" not in c.messages.calls[0]

    for exc, retryable in ((api_error(anthropic.RateLimitError, 429, "slow down"), True), (api_error(anthropic.InternalServerError, 500, "oops"), True),
                           (api_error(anthropic.AuthenticationError, 401, "bad key"), False), (api_error(anthropic.BadRequestError, 400, "bad schema"), False)):
        with pytest.raises(LLMError) as err:
            await AnthropicLLM(client=client(plain=[exc]), fallbacks=False).complete(REQ)
        assert err.value.retryable is retryable

    with pytest.raises(LLMError, match="declined"):
        await AnthropicLLM(client=client(plain=[message([], "refusal")]), fallbacks=False).complete(REQ)

    spent = AnthropicLLM(UsageLedger(max_calls=1), client=client(plain=[message([SimpleNamespace(type="text", text="a")])]), fallbacks=False)
    await spent.complete(REQ)
    with pytest.raises(BudgetExceeded):
        await spent.complete(REQ)


# --- Groq ---------------------------------------------------------------------------

import httpx  # noqa: E402

from incident_agents.llm import GroqLLM, LLMError, LLMRequest, RoleConfig, UsageLedger, detect_provider, to_openai_messages  # noqa: E402

GROQ_ROLES = {"specialist": RoleConfig("openai/gpt-oss-120b", "medium", 2000), "reasoning": RoleConfig("openai/gpt-oss-120b", "high", 2000)}


def groq(responses, **kw):
    """A GroqLLM whose HTTP calls are answered from a list of (status, json, headers)."""
    sent, slept = [], []

    def handler(request):
        sent.append(json.loads(request.content))
        status, body, headers = responses.pop(0)
        return httpx.Response(status, json=body, headers=headers)

    async def sleep(seconds):
        slept.append(seconds)

    llm = GroqLLM(UsageLedger(), GROQ_ROLES, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key="gsk_test", sleep=sleep, **kw)
    return llm, sent, slept


def ok(message, finish="stop", usage=None):
    return 200, {"model": "openai/gpt-oss-120b", "choices": [{"message": message, "finish_reason": finish}],
                 "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}}, {}


def test_conversation_blocks_become_chat_completion_roles():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "looking"}, {"type": "tool_use", "id": "c1", "name": "logs_count", "input": {"service": "api"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{\"status\": \"ok\"}"}, {"type": "text", "text": "Budget used up."}]},
    ]
    out = to_openai_messages("sys", messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[2]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {"name": "logs_count", "arguments": "{\"service\": \"api\"}"}}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "{\"status\": \"ok\"}"} and out[4]["content"] == "Budget used up."


async def test_groq_tool_calls_and_schema_handling():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
    tools = [{"name": "logs_count", "description": "count", "input_schema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}}]
    llm, sent, _ = groq([
        ok({"role": "assistant", "tool_calls": [{"id": "c9", "function": {"name": "logs_count", "arguments": "{\"service\": \"api\"}"}}]}, "tool_calls",
           {"prompt_tokens": 300, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 120}}),
        ok({"role": "assistant", "content": "{\"answer\": \"x\"}"}),
    ])
    resp = await llm.complete(LLMRequest("observability", "specialist", "sys", [{"role": "user", "content": "go"}], tools=tools, schema=schema))
    # With tools, structured output is not available: the schema goes into the instructions instead.
    assert "response_format" not in sent[0] and "matches this JSON Schema" in sent[0]["messages"][0]["content"]
    assert sent[0]["tools"][0]["function"]["parameters"]["required"] == ["service"] and sent[0]["reasoning_effort"] == "medium"
    assert [(c.id, c.name, c.input) for c in resp.tool_calls] == [("c9", "logs_count", {"service": "api"})] and resp.stop_reason == "tool_use"
    assert resp.content == [{"type": "tool_use", "id": "c9", "name": "logs_count", "input": {"service": "api"}}]
    assert (resp.usage.input_tokens, resp.usage.cache_read_tokens, resp.usage.output_tokens) == (180, 120, 40)

    resp = await llm.complete(LLMRequest("reasoning", "reasoning", "sys", [{"role": "user", "content": "go"}], schema=schema))
    assert sent[1]["response_format"]["json_schema"] == {"name": "answer", "schema": schema, "strict": True} and sent[1]["reasoning_effort"] == "high"
    assert resp.text == '{"answer": "x"}' and resp.stop_reason == "end_turn" and len(llm.ledger.calls) == 2


async def test_groq_waits_out_rate_limits_and_regenerates_malformed_output():
    rate = {"error": {"message": "Rate limit reached for model in organization org_abc123 on tokens per minute (TPM). Please try again in 7.5s.", "code": "rate_limit_exceeded"}}
    llm, sent, slept = groq([
        (429, rate, {"retry-after": "8"}),
        (400, {"error": {"message": "tool call validation failed", "code": "tool_use_failed"}}, {}),
        (503, {"error": {"message": "over capacity"}}, {}),
        ok({"role": "assistant", "content": "{}"}),
    ])
    resp = await llm.complete(LLMRequest("observability", "specialist", "sys", [{"role": "user", "content": "go"}]))
    assert resp.text == "{}" and len(sent) == 4 and 8 < slept[0] < 9.1 and slept[1] == 2.0

    # A wait longer than the allowance is an error the caller can report, not a hang.
    llm, _, slept = groq([(429, rate, {"retry-after": "3600"})], max_wait_s=60)
    with pytest.raises(LLMError, match="asks to wait 3600s") as exc:
        await llm.complete(LLMRequest("observability", "specialist", "sys", [{"role": "user", "content": "go"}]))
    assert "org_abc123" not in str(exc.value) and not slept

    # A request bigger than the per-minute limit can never succeed: fail at once and say why.
    big = {"error": {"message": "Request too large for model in organization org_abc123 on tokens per minute (TPM): Limit 8000, Requested 12000", "code": "rate_limit_exceeded"}}
    llm, sent, _ = groq([(413, big, {})])
    with pytest.raises(LLMError, match="larger than this Groq account") as exc:
        await llm.complete(LLMRequest("observability", "specialist", "sys", [{"role": "user", "content": "go"}]))
    assert exc.value.retryable is False and len(sent) == 1


async def test_groq_falls_back_from_strict_schemas_and_paces_from_headers():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    llm, sent, slept = groq([
        (400, {"error": {"message": "invalid JSON schema for response_format: strict mode requires additionalProperties false"}}, {}),
        (200, ok({"role": "assistant", "content": "{\"a\": \"1\"}"})[1], {"x-ratelimit-remaining-tokens": "50", "x-ratelimit-reset-tokens": "12.5s"}),
        ok({"role": "assistant", "content": "{}"}),
    ])
    llm.roles = {"reasoning": RoleConfig("pacing-test-model", "high", 100)}
    await llm.complete(LLMRequest("reasoning", "reasoning", "sys", [{"role": "user", "content": "go"}], schema=schema))
    assert "strict" in sent[0]["response_format"]["json_schema"] and "strict" not in sent[1]["response_format"]["json_schema"]
    assert "reasoning_effort" not in sent[0] and not slept
    # 50 tokens left in the window and the next request needs more: wait for the window to reset.
    await llm.complete(LLMRequest("reasoning", "reasoning", "sys " * 200, [{"role": "user", "content": "go"}]))
    assert len(slept) == 1 and 12 < slept[0] < 13


def test_provider_is_chosen_from_the_environment(monkeypatch):
    monkeypatch.delenv("IA_PROVIDER", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert detect_provider() == "anthropic"
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert detect_provider() == "groq"
    monkeypatch.setenv("IA_PROVIDER", "anthropic")
    assert detect_provider() == "anthropic"


async def test_other_openai_compatible_providers_and_a_server_without_structured_output(monkeypatch):
    from incident_agents.llm import OpenAICompatLLM, default_roles, have_credentials, make_llm

    sent, urls = [], []

    def handler(request):
        urls.append((str(request.url), request.headers.get("authorization")))
        sent.append(json.loads(request.content))
        if "response_format" in sent[-1]:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\": \"1\"}"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}})

    # A local Ollama server: no key, its own endpoint and model, and here one that refuses response_format.
    monkeypatch.setenv("IA_PROVIDER", "ollama")
    monkeypatch.delenv("IA_MODEL_STRONG", raising=False)
    monkeypatch.delenv("IA_BASE_URL", raising=False)
    assert have_credentials() and default_roles()["reasoning"].model == "gpt-oss:20b"
    llm = OpenAICompatLLM(UsageLedger(), provider="ollama", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": False}
    resp = await llm.complete(LLMRequest("reasoning", "reasoning", "sys", [{"role": "user", "content": "go"}], schema=schema))
    # strict, then best effort, then the schema in the instructions.
    assert len(sent) == 3 and "response_format" not in sent[2] and "matches this JSON Schema" in sent[2]["messages"][0]["content"]
    assert urls[0] == ("http://localhost:11434/v1/chat/completions", None) and sent[2]["max_tokens"] == 8000 and sent[2]["reasoning_effort"] == "high"
    assert resp.text == '{"a": "1"}' and llm.ledger.totals()["cost_usd"] >= 0

    monkeypatch.setenv("IA_PROVIDER", "nvidia")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    assert not have_credentials()
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-x")
    client = make_llm(UsageLedger())
    assert have_credentials() and client._url == "https://integrate.api.nvidia.com/v1/chat/completions" and client.roles["baseline"].model == "nvidia/nemotron-3-ultra-550b-a55b"

    # NVIDIA's free endpoint stalls on overlapping requests: calls are sent one at a time.
    live, peak = 0, 0

    async def slow(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}})

    one = OpenAICompatLLM(UsageLedger(), provider="nvidia", client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    await asyncio.gather(*(one.complete(LLMRequest("observability", "specialist", "sys", [{"role": "user", "content": "go"}])) for _ in range(5)))
    assert peak == 1 and len(one.ledger.calls) == 5


def test_ledger_stops_at_a_dollar_ceiling(monkeypatch):
    from incident_agents.llm import CallRecord, Usage, _price

    ledger = UsageLedger(max_cost_usd=0.05)
    ledger.record(CallRecord("a", "specialist", "claude-sonnet-5-5", Usage(input_tokens=10_000, output_tokens=1_000), 5, "end_turn"))
    assert ledger.cost_usd == pytest.approx(0.03) and not ledger.exhausted()
    ledger.record(CallRecord("a", "specialist", "claude-sonnet-5-5", Usage(input_tokens=10_000, output_tokens=1_000), 5, "end_turn"))
    assert ledger.exhausted()
    with pytest.raises(BudgetExceeded, match=r"\$0\.06"):
        ledger.check()
    monkeypatch.setenv("IA_MAX_COST_USD", "1.5")
    assert UsageLedger().max_cost_usd == 1.5
    monkeypatch.delenv("IA_MAX_COST_USD")
    assert UsageLedger().max_cost_usd is None
    assert _price("claude-sonnet-5-5-20261001") == _price("claude-sonnet-5-5") != _price("some-unknown-model")


async def test_schema_too_large_to_compile_moves_into_the_instructions():
    too_large = api_error(anthropic.BadRequestError, 400, "The compiled grammar is too large, which would cause performance issues.")
    ok = message([SimpleNamespace(type="text", text="{}")])
    c = client(plain=[too_large, ok, ok])
    llm = AnthropicLLM(client=c, fallbacks=False)
    assert (await llm.complete(REQ)).text == "{}"
    first, second = c.messages.calls[0], c.messages.calls[1]
    assert "format" in first["output_config"] and second["output_config"] == {"effort": "medium"}
    assert "JSON Schema" in second["system"][0]["text"] and second["tools"] == REQ.tools
    await llm.complete(REQ)  # remembered: no second refusal for the same schema
    assert len(c.messages.calls) == 3 and "format" not in c.messages.calls[2]["output_config"]
    other = LLMRequest("a", "specialist", "sys", [{"role": "user", "content": "x"}], schema={"type": "object", "properties": {"a": {"type": "string"}}})
    c.messages.outcomes.append(ok)
    await llm.complete(other)  # a different schema still gets constrained output
    assert "format" in c.messages.calls[3]["output_config"]
