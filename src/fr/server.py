"""Webhook receiver + live monitoring dashboard (FastAPI, Server-Sent Events)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .audit import BUS, read_trace, verify_trace
from .config import ROOT, Settings
from .killswitch import KillSwitch
from .runner import Agent
from .store import Store
from .tools.github import FakeGitHub, GitHub

FIXTURES = ROOT / "fixtures"
DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


class Service:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.ks = KillSwitch(settings.kill_file)
        self.store = Store(settings.state_dir)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.current: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []
        self.worker: asyncio.Task | None = None
        self.mock_delay = float(os.environ.get("MOCK_DELAY_S", "0.6"))

    def system(self, type_: str, **data: Any) -> None:
        BUS.publish({"run_id": "system", "seq": 0, "ts": round(time.time(), 3), "phase": "",
                     "type": type_, "data": data})

    def make_github(self, job: dict[str, Any]):
        if job["kind"] == "mock":
            fx = FIXTURES / "prs" / job["fixture"]
            if not fx.is_dir():
                raise HTTPException(404, f"unknown fixture {job['fixture']}")
            return FakeGitHub(fixture=fx, base_repo=FIXTURES / "base_repo", delay_s=self.mock_delay)
        return GitHub(self.s.github_token, self.s.github_repo)

    async def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        if self.ks.engaged:
            raise HTTPException(409, "kill switch engaged: resume before starting new runs")
        job["queued_at"] = time.time()
        await self.queue.put(job)
        self.system("job_queued", **{k: v for k, v in job.items() if k != "queued_at"})
        return {"queued": True, "position": self.queue.qsize()}

    async def work(self) -> None:
        while True:
            job = await self.queue.get()
            if self.ks.engaged:
                self.system("job_dropped", reason="kill switch engaged", **job)
                continue
            try:
                gh = self.make_github(job)
            except Exception as e:
                self.system("job_error", error=str(e), **job)
                continue
            mock = job["kind"] == "mock"
            agent = Agent(self.s, gh, self.ks, mock_mode=mock, store=self.store)
            pr = gh.meta.get("number") if mock else job.get("pr")
            task = asyncio.create_task(agent.run(pr, force=job.get("force", False)))
            self.ks.register(task)
            self.current = {"job": job, "started": time.time()}
            try:
                res = await task
                self.recent.insert(0, res.as_dict())
            except asyncio.CancelledError:
                if self.worker and self.worker.cancelling():
                    raise
            except Exception as e:  # the worker must survive anything a single run does
                self.system("job_error", error=f"{type(e).__name__}: {e}", **job)
            finally:
                self.current = None
                self.recent = self.recent[:20]
                try:
                    await gh.aclose()
                except Exception:  # noqa: BLE001 - closing must never kill the worker
                    pass

    def kill(self, reason: str) -> int:
        n = self.ks.engage(reason)
        dropped = 0
        while not self.queue.empty():
            self.queue.get_nowait()
            dropped += 1
        self.system("kill_switch", engaged=True, reason=reason, cancelled_runs=n, dropped_jobs=dropped)
        return n


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    svc = Service(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        svc.ks.start_watcher()
        svc.worker = asyncio.create_task(svc.work())
        yield
        svc.worker.cancel()

    app = FastAPI(title="First Responder", lifespan=lifespan)
    app.state.svc = svc

    @app.get("/")
    async def index():
        return FileResponse(DASHBOARD)

    @app.get("/api/state")
    async def state():
        fixtures = sorted(p.name for p in (FIXTURES / "prs").iterdir() if p.is_dir()) \
            if (FIXTURES / "prs").exists() else []
        return {"killed": svc.ks.engaged, "kill_reason": svc.ks.reason, "current": svc.current,
                "queue": svc.queue.qsize(), "recent": svc.recent[:10], "fixtures": fixtures,
                "live_configured": bool(settings.github_token and settings.github_repo),
                "repo": settings.github_repo, "llm": f"{settings.llm_provider}:{settings.llm_model}",
                "sandbox": f"live: {settings.sandbox} · mock: {settings.mock_sandbox}"}

    @app.get("/api/events")
    async def events(request: Request):
        q = BUS.subscribe()

        async def stream():
            try:
                for ev in list(BUS.history)[-400:]:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                BUS.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/api/kill")
    async def kill(request: Request):
        body = await _json(request)
        n = svc.kill(str(body.get("reason") or "dashboard kill button"))
        return {"killed": True, "cancelled_runs": n}

    @app.post("/api/resume")
    async def resume():
        svc.ks.release()
        svc.system("kill_switch", engaged=False)
        return {"killed": False}

    @app.post("/api/runs/mock")
    async def run_mock(request: Request):
        body = await _json(request)
        fixture = str(body.get("fixture", ""))
        if not (FIXTURES / "prs" / fixture).is_dir():
            raise HTTPException(404, "unknown fixture")
        return await svc.enqueue({"kind": "mock", "fixture": fixture, "pr": None})

    @app.post("/api/runs/live")
    async def run_live(request: Request):
        body = await _json(request)
        if not (settings.github_token and settings.github_repo):
            raise HTTPException(400, "live mode not configured (GITHUB_TOKEN / GITHUB_REPO)")
        return await svc.enqueue({"kind": "live", "pr": int(body["pr"]),
                                  "force": bool(body.get("force"))})

    @app.post("/webhook")
    async def webhook(request: Request):
        raw = await request.body()
        if not settings.webhook_secret:
            raise HTTPException(503, "WEBHOOK_SECRET not configured")
        sig = request.headers.get("x-hub-signature-256", "")
        expect = "sha256=" + hmac.new(settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            raise HTTPException(401, "bad signature")
        event = request.headers.get("x-github-event", "")
        payload = json.loads(raw or b"{}")
        if event == "ping":
            return {"pong": True}
        if event != "pull_request":
            return {"ignored": event}
        if payload.get("action") not in {"opened", "synchronize", "reopened", "ready_for_review"}:
            return {"ignored": payload.get("action")}
        if payload.get("repository", {}).get("full_name") != settings.github_repo:
            raise HTTPException(403, "repository is not the configured sandbox repo")
        pr = payload["pull_request"]
        if pr.get("draft"):
            return {"ignored": "draft"}
        return await svc.enqueue({"kind": "live", "pr": int(pr["number"]), "force": False})

    @app.get("/api/traces")
    async def traces():
        files = sorted(settings.traces_dir.glob("*.jsonl"), reverse=True)[:50]
        return [{"id": f.stem, "bytes": f.stat().st_size} for f in files]

    @app.get("/api/traces/{run_id}")
    async def trace(run_id: str):
        p = settings.traces_dir / f"{Path(run_id).name}.jsonl"
        if not p.exists():
            raise HTTPException(404, "no such trace")
        ok, msg = verify_trace(p)
        return JSONResponse({"verified": ok, "message": msg, "events": read_trace(p)})

    return app


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
