"""Provider code paths, exercised against a fake HTTP transport (no keys, no network)."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from fr.config import Settings
from fr.llm import LLM, LLMError, Plan

PLAN = {"summary": "s", "risk": "low", "checks": ["test"], "autofix": False}


def make(provider, handler, **kw):
    s = replace(Settings(), llm_provider=provider, llm_model="m", gemini_api_key="k",
                groq_api_key="k", **kw)
    return LLM(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def gemini_body(obj, tin=10, tout=5):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}],
            "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}}


def test_gemini_parses_and_counts_tokens():
    seen = {}

    def h(req):
        seen["key"] = req.headers.get("x-goog-api-key")
        seen["url"] = str(req.url)
        return httpx.Response(200, json=gemini_body(PLAN))
    llm = make("gemini", h, price_in_per_mtok=1.0, price_out_per_mtok=2.0)
    out, usage = asyncio.run(llm.complete("plan", "sys", "user", {}))
    assert isinstance(out, Plan) and out.risk == "low"
    assert usage.tokens_in == 10 and usage.tokens_out == 5
    assert llm.cost(usage) == pytest.approx(20e-6)
    assert seen["key"] == "k" and "key=" not in seen["url"]  # key in header, not URL


def test_groq_parses_fenced_json():
    def h(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(PLAN) + "\n```"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    out, usage = asyncio.run(make("groq", h).complete("plan", "s", "u", {}))
    assert out.summary == "s" and usage.tokens_out == 4


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def nosleep(_):
        return None
    monkeypatch.setattr("fr.llm.asyncio.sleep", nosleep)

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json=gemini_body(PLAN))
    out, _ = asyncio.run(make("gemini", h).complete("plan", "s", "u", {}))
    assert out.risk == "low" and calls["n"] == 3


def test_non_json_gets_one_repair_then_fails():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Sorry, no."}]}}]})
    with pytest.raises(LLMError):
        asyncio.run(make("gemini", h).complete("plan", "s", "u", {}))
    assert calls["n"] == 2


def test_messy_model_output_is_normalised_not_rejected():
    messy = {"summary": "x" * 5000, "risk": "Critical", "checks": ["Tests", "ruff", "deploy"],
             "risk_reasons": "one reason", "requested_actions": None}
    out, _ = asyncio.run(make("gemini", lambda r: httpx.Response(200, json=gemini_body(messy)))
                         .complete("plan", "s", "u", {}))
    assert out.risk == "high" and out.checks == ["test", "lint"]
    assert len(out.summary) == 1200 and out.risk_reasons == ["one reason"]


def test_bad_review_comments_are_dropped_individually():
    review = {"verdict": "ok", "comments": [
        {"path": "./calc/ops.py", "line": "3", "severity": "ERROR", "body": "bug"},
        {"path": "calc/ops.py", "line": "abc", "body": "no line"},
        {"file": "b/calc/x.py", "line_number": 2, "comment": "alt keys", "severity": "nit"},
        "not a dict"]}
    out, _ = asyncio.run(make("gemini", lambda r: httpx.Response(200, json=gemini_body(review)))
                         .complete("review", "s", "u", {}))
    assert [(c.path, c.line, c.severity) for c in out.comments] == [
        ("calc/ops.py", 3, "blocker"), ("calc/x.py", 2, "info")]


def test_gemini_falls_back_when_json_mode_field_is_rejected():
    seen = []

    def h(req):
        body = json.loads(req.content)
        seen.append(list(body.get("generationConfig", {}).keys()))
        if "responseFormat" in body.get("generationConfig", {}):
            return httpx.Response(400, json={"error": {"message": 'Invalid JSON payload received. Unknown name "responseFormat"'}})
        return httpx.Response(200, json=gemini_body(PLAN))
    llm = make("gemini", h)
    asyncio.run(llm.complete("plan", "s", "u", {}))
    asyncio.run(llm.complete("plan", "s", "u", {}))
    assert seen == [["responseFormat"], ["responseMimeType"], ["responseMimeType"]]  # remembered


def test_gemini_blocked_response_explains_why():
    def h(req):
        return httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]})
    with pytest.raises(LLMError, match="SAFETY"):
        asyncio.run(make("gemini", h).complete("plan", "s", "u", {}))


def test_unknown_model_gets_actionable_error():
    def h(req):
        return httpx.Response(404, json={"error": {"message": "models/x is not found"}})
    with pytest.raises(LLMError, match="LLM_MODEL"):
        asyncio.run(make("gemini", h).complete("plan", "s", "u", {}))


def test_groq_drops_json_mode_if_unsupported():
    calls = []

    def h(req):
        body = json.loads(req.content)
        calls.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(PLAN)}}]})
    out, _ = asyncio.run(make("groq", h).complete("plan", "s", "u", {}))
    assert out.risk == "low" and calls == [True, False]


def test_default_models_are_current(monkeypatch):
    """Checked against the Gemini and Groq deprecation pages on 2026-11-26."""
    import fr.config
    monkeypatch.setattr(fr.config, "load_dotenv", lambda *a, **k: None)  # ignore a local .env
    monkeypatch.delenv("LLM_MODEL", raising=False)
    for provider, model in (("gemini", "gemini-3.8-flash"), ("groq", "openai/gpt-oss-120b")):
        monkeypatch.setenv("LLM_PROVIDER", provider)
        assert fr.config.Settings.from_env().llm_model == model


def test_auth_error_is_not_retried():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(403, json={"error": "bad key"})
    with pytest.raises(LLMError):
        asyncio.run(make("gemini", h).complete("plan", "s", "u", {}))
    assert calls["n"] == 1


def test_missing_key_fails_clearly():
    llm = make("gemini", lambda r: httpx.Response(200))
    llm.s.gemini_api_key = ""
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        asyncio.run(llm.complete("plan", "s", "u", {}))
