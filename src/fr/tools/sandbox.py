"""Run the target repo's checks in isolation.

Only three command *keys* exist: test, lint, fix. Their actual command lines come
from the target repo's .first-responder.toml (or the defaults below), never from
the model and never from PR text.

docker backend (live mode): no network, capped memory/CPU/pids, all capabilities
dropped, read-only root filesystem, never pulls images, container killed on
cancel/timeout.

local backend: plain subprocess with a scrubbed environment. Refused unless the
run is in mock mode, because it runs PR code on the host.

Infrastructure problems (Docker not running, image missing, tool not installed)
raise SandboxUnavailable. They are never reported as "the PR's tests failed".
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import shlex
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COMMANDS = {
    "test": "python -m pytest -q -x --no-header -p no:cacheprovider",
    "lint": "ruff check --output-format=concise --no-cache .",
    "fix": "ruff check --fix --no-cache . && ruff format --no-cache .",
}
SAFE_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                 "PATHEXT", "COMSPEC", "LANG", "LC_ALL"}
LINT_LINE = re.compile(r"^(?P<path>[^:\s][^:]*):(?P<line>\d+):(?P<col>\d+): (?P<code>[A-Z]+\d+) (?P<msg>.*)$")
DOCKER_INFRA_ERRORS = re.compile(
    r"(Cannot connect to the Docker daemon|error during connect|docker daemon is not running|"
    r"Unable to find image|No such image|pull access denied|failed to connect to the docker API|"
    r"invalid reference format|is not recognized as an internal or external command)", re.IGNORECASE)
PYTEST_NO_TESTS = 5


class SandboxRefused(Exception):
    """The sandbox will not run this (policy)."""


class SandboxUnavailable(SandboxRefused):
    """The sandbox cannot run at all (infrastructure). Never blamed on the PR."""


@dataclass
class CheckResult:
    command: str
    ok: bool
    exit_code: int
    duration_s: float
    output: str
    first_failure: str | None = None
    lint_issues: list[dict] = field(default_factory=list)
    timed_out: bool = False
    note: str | None = None

    def brief(self) -> dict:
        return {"command": self.command, "ok": self.ok, "exit_code": self.exit_code,
                "duration_s": self.duration_s, "timed_out": self.timed_out,
                "first_failure": self.first_failure, "lint_issues": len(self.lint_issues),
                "note": self.note}


def load_commands(workspace: Path) -> dict[str, str]:
    cfg = workspace / ".first-responder.toml"
    cmds = dict(DEFAULT_COMMANDS)
    if cfg.exists():
        try:
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            for k in DEFAULT_COMMANDS:
                v = data.get("commands", {}).get(k)
                if isinstance(v, str) and v.strip():
                    cmds[k] = v.strip()
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            pass  # fall back to defaults; a broken config must not break the review
    return cmds


def parse_lint(output: str) -> list[dict]:
    issues = []
    for line in output.splitlines():
        m = LINT_LINE.match(line.strip())
        if m:
            issues.append({"path": m["path"].replace("\\", "/").removeprefix("./"),
                           "line": int(m["line"]), "code": m["code"], "message": m["msg"]})
    return issues


def first_test_failure(output: str) -> str | None:
    lines = output.splitlines()
    for ln in lines:
        if ln.startswith(("FAILED ", "ERROR ")):
            return ln[:300]
    for ln in lines:
        if ln.lstrip().startswith("E ") or "Error" in ln:
            return ln.strip()[:300]
    return None


class Sandbox:
    def __init__(self, backend: str, image: str, timeout_s: int, mock_mode: bool,
                 run_id: str = "run") -> None:
        if backend == "local" and not mock_mode:
            raise SandboxRefused("SANDBOX=local runs PR code on the host; allowed only in mock mode")
        if backend not in ("docker", "local"):
            raise SandboxRefused(f"unknown sandbox backend '{backend}'")
        self.backend, self.image, self.timeout_s, self.run_id = backend, image, timeout_s, run_id
        self._seq = itertools.count(1)

    async def run(self, key: str, workspace: Path) -> CheckResult:
        cmds = load_commands(workspace)
        if key not in cmds:
            raise SandboxRefused(f"no command for '{key}'")
        cmd = cmds[key]
        start = time.monotonic()
        if self.backend == "docker":
            code, out, timed_out = await self._docker(cmd, workspace, key)
        else:
            code, out, timed_out = await self._local(cmd, workspace)
        dur = round(time.monotonic() - start, 2)
        res = CheckResult(command=key, ok=(code == 0 and not timed_out), exit_code=code,
                          duration_s=dur, output=out[-8000:], timed_out=timed_out)
        if key == "lint":
            res.lint_issues = parse_lint(out)
        if key == "test" and not res.ok:
            if code == PYTEST_NO_TESTS and not timed_out:
                res.note = "no tests were collected"
                res.first_failure = "no tests were collected"
            else:
                res.first_failure = "timed out" if timed_out else first_test_failure(out)
        return res

    async def _docker(self, cmd: str, ws: Path, key: str) -> tuple[int, str, bool]:
        name = f"fr-{self.run_id}-{key}-{next(self._seq)}"[:63]
        args = ["docker", "run", "--rm", "--pull", "never", "--name", name, "--network", "none",
                "--memory", "768m", "--cpus", "1", "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,size=128m",
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp"]
        if hasattr(os, "getuid"):  # POSIX: files written in the workspace stay owned by us
            args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        args += ["-v", f"{ws.resolve()}:/work", "-w", "/work", self.image, "sh", "-c", cmd]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as e:
            raise SandboxUnavailable("docker is not installed or not on PATH") from e
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except TimeoutError:
            await _docker_kill(name, proc)
            return 124, "sandbox timeout", True
        except asyncio.CancelledError:
            await asyncio.shield(_docker_kill(name, proc))
            raise
        out = out_b.decode("utf-8", "replace")
        code = proc.returncode or 0
        # 125 = docker itself failed, 126 = cannot execute, 127 = command not found in the image
        if code in (125, 126, 127) or (code != 0 and DOCKER_INFRA_ERRORS.search(out[:300])):
            hint = " Run `fr sandbox-build`." if re.search(r"Unable to find image|No such image|pull access", out, re.I) \
                else " Is Docker Desktop running? Try `fr doctor`."
            raise SandboxUnavailable(f"docker sandbox failed (exit {code}): {out.strip()[:200]}.{hint}")
        return code, out, False

    async def _local(self, cmd: str, ws: Path) -> tuple[int, str, bool]:
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        code_total, outputs = 0, []
        for part in [p.strip() for p in cmd.split("&&")]:
            argv = shlex.split(part, posix=True)
            if argv and argv[0] == "python":
                argv[0] = sys.executable
            elif argv and argv[0] in ("ruff", "pytest"):
                argv = [sys.executable, "-m", argv[0], *argv[1:]]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(ws), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except FileNotFoundError as e:
                raise SandboxUnavailable(f"command not found in local sandbox: {argv[:1]}") from e
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
            except TimeoutError:
                await _kill_and_reap(proc)
                return 124, "\n".join(outputs) + "\nsandbox timeout", True
            except asyncio.CancelledError:
                await _kill_and_reap(proc)
                raise
            outputs.append(out.decode("utf-8", "replace"))
            code_total = proc.returncode or 0
            if code_total != 0:
                break
        return code_total, "\n".join(outputs), False


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill, then wait (briefly) for the OS to release the process's file handles."""
    _kill(proc)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def _docker_kill(name: str, proc: asyncio.subprocess.Process) -> None:
    try:
        k = await asyncio.create_subprocess_exec("docker", "kill", name,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(k.wait(), timeout=10)
    except (TimeoutError, OSError):
        pass
    await _kill_and_reap(proc)
