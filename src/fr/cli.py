"""Command line entry point: `fr <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .audit import EventBus, read_trace, verify_trace
from .config import ROOT, Settings
from .fmt import format_event
from .killswitch import KillSwitch

FIXTURES = ROOT / "fixtures"


class PrintBus(EventBus):
    def __init__(self, color: bool = True) -> None:
        super().__init__()
        self.color = color

    def publish(self, event):  # type: ignore[override]
        super().publish(event)
        print(format_event(event, self.color), flush=True)


def _color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        os.system("")  # enable ANSI escapes in the Windows console
    return sys.stdout.isatty()


async def _run(settings: Settings, args) -> int:
    from .runner import Agent
    from .store import Store
    from .tools.github import FakeGitHub, GitHub

    ks = KillSwitch(settings.kill_file)
    if ks.engaged:
        print("kill switch is engaged (.fr/KILL). Run `fr resume` first.")
        return 3
    mock = bool(args.mock)
    if mock:
        fx = FIXTURES / "prs" / args.mock
        if not fx.is_dir():
            print(f"unknown fixture '{args.mock}'. Try: fr fixtures")
            return 2
        gh = FakeGitHub(fixture=fx, base_repo=FIXTURES / "base_repo", delay_s=args.delay)
        args.pr = gh.meta.get("number")
    else:
        if args.pr is None:
            print("give --pr N (live) or --mock FIXTURE")
            return 2
        gh = GitHub(settings.github_token, settings.github_repo)
    agent = Agent(settings, gh, ks, mock_mode=mock, bus=PrintBus(_color()),
                  store=Store(settings.state_dir))
    ks.start_watcher()
    task = asyncio.create_task(agent.run(args.pr, force=args.force))
    ks.register(task)
    try:
        res = await task
    finally:
        await gh.aclose()
    if mock and args.show_actions:
        print("\n--- recorded GitHub writes (mock) ---")
        for a in gh.actions:
            print(json.dumps(a, indent=2, default=str)[:3000])
    print(f"\ntrace: {res.trace}")
    return 0 if res.status in ("DONE", "NEEDS_HUMAN", "SKIPPED") else 1


def cmd_replay(path: Path, speed: float) -> int:
    ok, msg = verify_trace(path)
    events = read_trace(path)
    prev_ts = events[0]["ts"] if events else 0
    color = _color()
    for ev in events:
        if speed > 0:
            time.sleep(min((ev["ts"] - prev_ts) / speed, 2.0))
        prev_ts = ev["ts"]
        print(format_event(ev, color))
    print(f"\nintegrity: {'OK' if ok else 'FAILED'}: {msg}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fr", description="First Responder: PR first-pass agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="webhook receiver + live dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)

    r = sub.add_parser("run", help="run once on a PR (live) or a fixture (mock)")
    r.add_argument("--pr", type=int)
    r.add_argument("--mock", metavar="FIXTURE")
    r.add_argument("--force", action="store_true", help="ignore the idempotency store")
    r.add_argument("--delay", type=float, default=0.0, help="simulated API latency (mock)")
    r.add_argument("--show-actions", action="store_true", help="print recorded writes (mock)")

    sub.add_parser("fixtures", help="list mock PR fixtures")
    rp = sub.add_parser("replay", help="replay a trace and verify its hash chain")
    rp.add_argument("trace")
    rp.add_argument("--speed", type=float, default=0, help="0 = instant, 1 = real time")
    v = sub.add_parser("verify", help="verify a trace's hash chain")
    v.add_argument("trace")
    k = sub.add_parser("kill", help="engage the kill switch (stops running agents)")
    k.add_argument("reason", nargs="*")
    sub.add_parser("resume", help="release the kill switch")
    tp = sub.add_parser("trap-pack", help="run the trap pack and write tests/trap_pack/RESULTS.md")
    tp.add_argument("--dir", default=str(ROOT / "tests" / "trap_pack"))
    sub.add_parser("sandbox-build", help="build the Docker sandbox image")
    sub.add_parser("doctor", help="check model key, GitHub token, Docker and setup before a run")
    sd = sub.add_parser("seed", help="live setup: push the sandbox app to an empty repo and open demo PRs")
    sd.add_argument("--fixtures", default="clean,lint,risky,hostile",
                    help="comma-separated fixture names to open as PRs (default: %(default)s)")
    sd.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    settings = Settings.from_env()

    if args.cmd == "serve":
        import uvicorn

        from .server import create_app
        print(f"First Responder dashboard: http://{args.host}:{args.port}")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd == "run":
        return asyncio.run(_run(settings, args))
    if args.cmd == "fixtures":
        for d in sorted((FIXTURES / "prs").iterdir()):
            meta = json.loads((d / "pr.json").read_text(encoding="utf-8"))
            print(f"{d.name:<14} #{meta.get('number')}  {meta.get('title')}")
        return 0
    if args.cmd == "replay":
        return cmd_replay(_trace_path(settings, args.trace), args.speed)
    if args.cmd == "verify":
        ok, msg = verify_trace(_trace_path(settings, args.trace))
        print(("OK: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1
    if args.cmd == "kill":
        KillSwitch(settings.kill_file).engage(" ".join(args.reason) or "cli")
        print(f"kill switch ENGAGED ({settings.kill_file}). Running agents stop within ~0.5s.")
        return 0
    if args.cmd == "resume":
        KillSwitch(settings.kill_file).release()
        print("kill switch released")
        return 0
    if args.cmd == "trap-pack":
        from .trappack import run_trap_pack
        return asyncio.run(run_trap_pack(settings, Path(args.dir)))
    if args.cmd == "sandbox-build":
        return subprocess.call(["docker", "build", "-t", settings.sandbox_image,
                                "-f", str(ROOT / "sandbox.Dockerfile"), str(ROOT)])
    if args.cmd == "doctor":
        from .doctor import run_doctor
        return asyncio.run(run_doctor(settings))
    if args.cmd == "seed":
        if not (settings.github_token and settings.github_repo):
            print("set GITHUB_TOKEN and GITHUB_REPO in .env first (see README, live mode)")
            return 2
        names = [n.strip() for n in args.fixtures.split(",") if n.strip()]
        print(f"This pushes branches and opens {len(names)} PR(s) in {settings.github_repo}: {', '.join(names)}")
        if not args.yes:
            if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() != "y":
                print("aborted")
                return 1
        from .seed import run_seed
        return asyncio.run(run_seed(settings, names))
    return 2


def _trace_path(settings: Settings, ref: str) -> Path:
    p = Path(ref)
    if p.exists():
        return p
    cand = settings.traces_dir / f"{ref}.jsonl"
    if cand.exists():
        return cand
    raise SystemExit(f"trace not found: {ref}")


if __name__ == "__main__":
    sys.exit(main())
