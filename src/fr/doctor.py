"""`fr doctor`: check every dependency of a run before you (or a judge) start one."""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys

from .config import ROOT, Settings
from .killswitch import KillSwitch
from .llm import LLM, LLMError
from .tools.github import GitHub, GitHubError

OK, WARN, FAIL = "OK", "WARN", "FAIL"


async def _cmd(*args: str, timeout: float = 20) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def run_doctor(s: Settings) -> int:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, name: str, detail: str) -> None:
        rows.append((status, name, detail))

    # --- basics
    v = sys.version_info
    add(OK if v >= (3, 11) else FAIL, "python", f"{v.major}.{v.minor}.{v.micro}"
        + ("" if v >= (3, 11) else " (needs 3.11+)"))
    code, out = await _cmd("git", "--version")
    add(OK if code == 0 else FAIL, "git", out if code == 0 else "git is not installed")
    for mod in ("pytest", "ruff"):
        add(OK if importlib.util.find_spec(mod) else FAIL, f"mock sandbox: {mod}",
            "installed" if importlib.util.find_spec(mod) else "missing: run `uv sync`")
    fixtures = ROOT / "fixtures" / "prs"
    add(OK if fixtures.is_dir() else FAIL, "mock fixtures",
        f"{len(list(fixtures.iterdir()))} demo PRs" if fixtures.is_dir() else "fixtures/ missing")
    try:
        s.traces_dir.mkdir(parents=True, exist_ok=True)
        probe = s.traces_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        add(OK, "traces dir", str(s.traces_dir))
    except OSError as e:
        add(FAIL, "traces dir", f"not writable: {e}")
    ks = KillSwitch(s.kill_file)
    add(WARN if ks.engaged else OK, "kill switch",
        "ENGAGED: runs are refused until `fr resume`" if ks.engaged else "released")

    # --- model
    llm = LLM(s)
    try:
        add(OK, f"model ({llm.describe()})", await llm.ping())
    except LLMError as e:
        add(FAIL, f"model ({llm.describe()})", str(e))
    finally:
        await llm.aclose()

    # --- live GitHub
    live = bool(s.github_token and s.github_repo)
    if not live:
        add(WARN, "github (live mode)", "GITHUB_TOKEN / GITHUB_REPO not set: mock mode only")
    else:
        gh = GitHub(s.github_token, s.github_repo)
        try:
            info = await gh.repo_info()
            perms = info.get("permissions") or {}
            detail = (f"{info.get('full_name')} · default branch '{info.get('default_branch')}' · "
                      f"{'private' if info.get('private') else 'public'}")
            add(OK if perms.get("push", True) else FAIL, "github repo",
                detail if perms.get("push", True) else detail + " · token cannot push")
        except GitHubError as e:
            add(FAIL, "github repo", str(e)[:200])
        finally:
            await gh.aclose()
        add(OK if s.webhook_secret else WARN, "webhook secret",
            "set" if s.webhook_secret else "WEBHOOK_SECRET not set: /webhook will refuse events")

    # --- docker sandbox (required for live runs)
    need = FAIL if live else WARN
    if not shutil.which("docker"):
        add(need, "docker", "not installed (live runs need it; mock runs do not)")
    else:
        code, out = await _cmd("docker", "info", "--format", "{{.ServerVersion}}")
        if code != 0:
            add(need, "docker daemon", "not running: start Docker Desktop")
        else:
            add(OK, "docker daemon", f"server {out}")
            code, _ = await _cmd("docker", "image", "inspect", s.sandbox_image)
            add(OK if code == 0 else need, "sandbox image",
                s.sandbox_image if code == 0 else f"{s.sandbox_image} missing: run `fr sandbox-build`")
    add(OK if s.sandbox == "docker" else FAIL, "live sandbox backend",
        s.sandbox if s.sandbox == "docker" else f"'{s.sandbox}': live runs require SANDBOX=docker")

    width = max(len(r[1]) for r in rows)
    icon = {OK: "✓", WARN: "!", FAIL: "✗"}
    for status, name, detail in rows:
        print(f" {icon[status]} {status:<4} {name:<{width}}  {detail}")
    fails = sum(1 for r in rows if r[0] == FAIL)
    mode = "live + mock" if live else "mock"
    print(f"\n{'All good' if not fails else f'{fails} problem(s)'} · ready for: "
          f"{mode if not fails else 'fix the FAIL lines first'}")
    return 1 if fails else 0
