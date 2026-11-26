"""Webhook receiver and dashboard API (FastAPI TestClient; no network)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fr.server import create_app

SECRET = "s3cret-for-tests"
REPO = "sandbox/fr-sandbox"


@pytest.fixture
def client(settings):
    app = create_app(replace(settings, webhook_secret=SECRET, github_repo=REPO, github_token="t"))
    jobs: list[dict] = []

    async def capture(job):  # never start a real live run from a unit test
        jobs.append(job)
        return {"queued": True}
    app.state.svc.enqueue = capture
    with TestClient(app) as c:
        c.jobs = jobs
        yield c


def post_hook(client, payload: dict, event: str = "pull_request", secret: str = SECRET, sign: bool = True):
    raw = json.dumps(payload).encode()
    headers = {"x-github-event": event, "content-type": "application/json"}
    if sign:
        headers["x-hub-signature-256"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers=headers)


def pr_event(action="opened", repo=REPO, draft=False, number=7):
    return {"action": action, "repository": {"full_name": repo},
            "pull_request": {"number": number, "draft": draft}}


def test_opened_pr_is_queued(client):
    r = post_hook(client, pr_event())
    assert r.status_code == 200 and client.jobs == [{"kind": "live", "pr": 7, "force": False}]


@pytest.mark.parametrize("action", ["synchronize", "reopened", "ready_for_review"])
def test_other_triggering_actions(client, action):
    assert post_hook(client, pr_event(action)).status_code == 200 and len(client.jobs) == 1


def test_unsigned_or_wrongly_signed_is_rejected(client):
    assert post_hook(client, pr_event(), sign=False).status_code == 401
    assert post_hook(client, pr_event(), secret="wrong").status_code == 401
    assert client.jobs == []


def test_other_repo_is_refused(client):
    assert post_hook(client, pr_event(repo="someone/else")).status_code == 403
    assert client.jobs == []


@pytest.mark.parametrize("payload,event", [
    (pr_event("closed"), "pull_request"), (pr_event(draft=True), "pull_request"),
    ({"zen": "hi"}, "ping"), ({"action": "created"}, "issue_comment")])
def test_ignored_events(client, payload, event):
    assert post_hook(client, payload, event=event).status_code == 200
    assert client.jobs == []


def test_webhook_refuses_without_secret(settings):
    app = create_app(replace(settings, webhook_secret="", github_repo=REPO))
    with TestClient(app) as c:
        assert c.post("/webhook", content=b"{}", headers={"x-github-event": "ping"}).status_code == 503


def test_kill_and_resume_endpoints(client, settings):
    assert client.post("/api/kill", json={"reason": "test"}).json()["killed"] is True
    assert client.get("/api/state").json()["killed"] is True
    client.post("/api/resume")
    assert client.get("/api/state").json()["killed"] is False


def test_state_lists_fixtures_and_dashboard_loads(client):
    state = client.get("/api/state").json()
    names = {f["name"] for f in state["fixtures"]}
    assert {"clean", "hostile", "slow"} <= names
    assert all(f["demo"] and f["number"] for f in state["fixtures"]), "every demo PR explains itself"
    assert "KILL SWITCH" in client.get("/").text


def test_run_history_summarises_traces(client, settings, make_agent):
    import asyncio
    agent, _, _ = make_agent("hostile")
    asyncio.run(agent.run(None))
    runs = client.get("/api/runs").json()
    assert runs and runs[0]["status"] == "DONE" and runs[0]["risk"] == "high"
    assert runs[0]["title"] == "Docs: add usage examples" and runs[0]["blocked"] >= 3
    trace = client.get(f"/api/traces/{runs[0]['id']}").json()
    assert trace["verified"] is True and trace["events"][-1]["type"] == "run_finished"


@pytest.mark.parametrize("body", [{}, {"pr": "abc"}, {"pr": 0}, {"pr": None}])
def test_live_run_rejects_bad_pr_numbers(client, body):
    assert client.post("/api/runs/live", json=body).status_code == 400


def test_unknown_trace_is_404_and_path_traversal_is_contained(client):
    assert client.get("/api/traces/does-not-exist").status_code == 404
    assert client.get("/api/traces/..%2F..%2Fpyproject").status_code == 404
