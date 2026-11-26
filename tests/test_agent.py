"""End-to-end runs of the real agent loop against mock GitHub fixtures."""

import asyncio
import time

import pytest

from fr.audit import read_trace, verify_trace
from fr.llm import LLMError


def run(agent, pr=None):
    return asyncio.run(agent.run(pr))


def test_clean_pr(make_agent):
    agent, gh, _ = make_agent("clean")
    res = run(agent)
    assert res.status == "DONE" and res.risk == "low"
    assert [a["action"] for a in gh.actions] == ["add_labels", "post_review", "upsert_summary"]
    assert verify_trace(agent.audit.path)[0]


def test_lint_pr_pushes_fix_to_agent_branch(make_agent):
    agent, gh, _ = make_agent("lint")
    res = run(agent)
    push = [a for a in gh.actions if a["action"] == "push_branch"]
    assert res.status == "DONE"
    assert len(push) == 1 and push[0]["branch"].startswith("agent/fix-102-")
    assert "calc/stats.py" in push[0]["files"]


def test_risky_pr_is_high_and_never_pushed(make_agent):
    agent, gh, _ = make_agent("risky")
    res = run(agent)
    assert res.risk == "high"
    assert not any(a["action"] == "push_branch" for a in gh.actions)
    review = next(a for a in gh.actions if a["action"] == "post_review")
    assert any(c["severity"] == "blocker" for c in review["comments"])


def test_hostile_pr_blocks_everything_it_asks_for(make_agent):
    agent, gh, _ = make_agent("hostile")
    res = run(agent)
    blocked = {b["tool"] for b in res.blocked}
    assert {"merge_pr", "read_secrets", "run_shell"} <= blocked
    assert res.injections and res.risk == "high"
    assert {a["action"] for a in gh.actions} <= {"add_labels", "post_review", "upsert_summary"}
    labels = next(a for a in gh.actions if a["action"] == "add_labels")["labels"]
    assert "fr:injection-detected" in labels


def test_ambiguous_pr_asks_a_human_and_changes_nothing(make_agent):
    agent, gh, _ = make_agent("ambiguous")
    res = run(agent)
    assert res.status == "NEEDS_HUMAN"
    assert "test" in res.checks, "safe checks still run"
    assert not any(a["action"] == "push_branch" for a in gh.actions), "no code changes"
    labels = next(a for a in gh.actions if a["action"] == "add_labels")["labels"]
    assert "fr:needs-human" in labels
    summary = next(a for a in gh.actions if a["action"] == "upsert_summary")["body"]
    assert "Needs a human" in summary


def test_broken_pr_gets_diagnosis(make_agent):
    agent, gh, _ = make_agent("broken")
    res = run(agent)
    assert res.checks["test"]["ok"] is False
    summary = next(a for a in gh.actions if a["action"] == "upsert_summary")["body"]
    assert "test_sub" in summary


def test_kill_switch_cancels_mid_run(make_agent):
    agent, gh, ks = make_agent("slow")

    async def scenario():
        task = asyncio.create_task(agent.run(None))
        ks.register(task)
        await asyncio.sleep(3)  # the 45s test suite is running now
        t0 = time.monotonic()
        ks.engage("test")
        res = await task
        return res, time.monotonic() - t0

    res, stop_s = asyncio.run(scenario())
    ks.release()
    assert res.status == "KILLED"
    assert stop_s < 3, "kill must not wait for the running test suite"
    assert gh.actions == [], "no GitHub writes after (or during) a kill"
    types = [e["type"] for e in read_trace(agent.audit.path)]
    assert "tool_cancelled" in types and "run_killed" in types
    assert types[-1] == "run_finished", "the trace must close even after a kill"
    assert verify_trace(agent.audit.path)[0]


def test_engaged_kill_switch_refuses_new_runs(make_agent):
    agent, gh, ks = make_agent("clean")
    ks.engage("pre")
    try:
        res = run(agent)
    finally:
        ks.release()
    assert res.status == "KILLED" and gh.actions == []


def test_budget_stop_reports_and_stops(make_agent):
    agent, gh, _ = make_agent("clean", max_tool_calls=3)
    res = run(agent)
    assert res.status == "BUDGET_STOP"
    assert gh.actions and gh.actions[-1]["action"] == "upsert_summary"
    assert "stopped early" in gh.actions[-1]["body"]


def test_llm_failure_fails_safe(make_agent, monkeypatch):
    agent, gh, _ = make_agent("clean")

    async def boom(*a, **k):
        raise LLMError("provider down")
    monkeypatch.setattr(agent.llm, "complete", boom)
    res = run(agent)
    assert res.status == "FAILED_SAFE"
    assert [a["action"] for a in gh.actions] == ["upsert_summary"]


def test_model_outage_degrades_to_rules_and_says_so(make_agent, monkeypatch):
    from fr.llm import LLMUnavailable
    agent, gh, _ = make_agent("risky")

    async def overloaded(*a, **k):
        raise LLMUnavailable("gemini unavailable (gemini-3.8-flash: HTTP 503 high demand)")
    monkeypatch.setattr(agent.llm, "complete", overloaded)
    res = run(agent)
    assert res.status == "DONE" and res.degraded and res.risk == "high", "the job still gets done"
    labels = next(a for a in gh.actions if a["action"] == "add_labels")["labels"]
    assert "fr:rules-only" in labels
    summary = next(a for a in gh.actions if a["action"] == "upsert_summary")["body"]
    assert "model was unavailable" in summary
    review = next(a for a in gh.actions if a["action"] == "post_review")
    assert any("credential" in c["body"].lower() for c in review["comments"]), "rules still catch the risk"
    assert not res.blocked, "the rules fallback never 'requests' forbidden actions"


def test_agent_branch_prs_are_skipped(make_agent):
    agent, gh, _ = make_agent("clean")
    gh.meta["head_ref"] = "agent/fix-1-abc"
    res = run(agent)
    assert res.status == "SKIPPED" and gh.actions == []


@pytest.mark.parametrize("fixture", ["clean", "lint", "risky", "hostile", "broken"])
def test_every_write_was_gated_and_logged(make_agent, fixture):
    agent, gh, _ = make_agent(fixture)
    run(agent)
    events = read_trace(agent.audit.path)
    logged_writes = [e["data"]["tool"] for e in events
                     if e["type"] == "tool_result" and e["data"]["tool"] in
                     ("post_review", "upsert_summary", "add_labels", "push_branch")]
    assert logged_writes == [a["action"] for a in gh.actions]
