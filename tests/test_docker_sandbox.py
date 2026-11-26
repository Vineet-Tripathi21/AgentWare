"""The real Docker sandbox. Skipped (and reported as skipped) where Docker is not available.

CI builds the image and runs these on every push (.github/workflows/ci.yml, job docker-sandbox).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fr.config import ROOT
from fr.tools.github import apply_fixture
from fr.tools.sandbox import Sandbox, SandboxUnavailable

IMAGE = "fr-sandbox:latest"
FIXTURES = ROOT / "fixtures"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _image_ok() -> bool:
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


DOCKER_OK = _docker_ok()
IMAGE_OK = DOCKER_OK and _image_ok()

# CI sets FR_REQUIRE_DOCKER=1. There a skip would hide a broken sandbox behind a green check,
# so missing Docker is a hard collection error instead.
if os.environ.get("FR_REQUIRE_DOCKER") == "1" and not IMAGE_OK:
    raise RuntimeError(f"FR_REQUIRE_DOCKER=1 but Docker or the image {IMAGE} is not available")

needs_docker = pytest.mark.skipif(not DOCKER_OK, reason="Docker daemon not available")
needs_image = pytest.mark.skipif(not IMAGE_OK, reason=f"Docker or image {IMAGE} not available (run `fr sandbox-build`)")


def workspace(tmp_path: Path, fixture: str) -> Path:
    ws = tmp_path / fixture
    apply_fixture(FIXTURES / "base_repo", FIXTURES / "prs" / fixture, ws)
    return ws


@needs_image
def test_docker_runs_checks_and_fix(tmp_path):
    ws = workspace(tmp_path, "lint")
    sb = Sandbox("docker", IMAGE, 120, mock_mode=False, run_id="t1")
    test = asyncio.run(sb.run("test", ws))
    lint = asyncio.run(sb.run("lint", ws))
    assert test.ok, test.output
    assert not lint.ok and lint.lint_issues, lint.output
    before = (ws / "calc" / "stats.py").read_text()
    fix = asyncio.run(sb.run("fix", ws))
    assert fix.exit_code in (0, 1)
    assert (ws / "calc" / "stats.py").read_text() != before, "fixer must edit the mounted workspace"
    assert asyncio.run(sb.run("test", ws)).ok


@needs_image
def test_docker_failing_tests_are_a_result_not_an_infra_error(tmp_path):
    ws = workspace(tmp_path, "broken")
    res = asyncio.run(Sandbox("docker", IMAGE, 120, False, "t2").run("test", ws))
    assert not res.ok and "test_sub" in (res.first_failure or "")


@needs_image
def test_docker_has_no_network(tmp_path):
    ws = workspace(tmp_path, "clean")
    (ws / ".first-responder.toml").write_text(
        '[commands]\ntest = "python -c \\"import urllib.request as u; u.urlopen(\'https://example.com\', timeout=5)\\""\n')
    res = asyncio.run(Sandbox("docker", IMAGE, 60, False, "t3").run("test", ws))
    assert not res.ok, "PR code must not reach the network from the sandbox"


@needs_image
def test_docker_kill_removes_the_container(tmp_path):
    ws = workspace(tmp_path, "slow")
    sb = Sandbox("docker", IMAGE, 120, False, "t4kill")

    async def scenario():
        task = asyncio.create_task(sb.run("test", ws))
        await asyncio.sleep(4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    left = subprocess.run(["docker", "ps", "-aq", "--filter", "name=fr-t4kill"],
                          capture_output=True, text=True).stdout.strip()
    assert left == "", "cancelled container must be gone"


@needs_docker
def test_missing_image_is_infra_error_not_test_failure(tmp_path):
    ws = workspace(tmp_path, "clean")
    sb = Sandbox("docker", "fr-sandbox-does-not-exist:latest", 60, False, "t5")
    with pytest.raises(SandboxUnavailable, match="sandbox-build|Docker"):
        asyncio.run(sb.run("test", ws))


def test_docker_missing_binary_is_infra_error(tmp_path, monkeypatch):
    """Runs everywhere: no docker on PATH must never look like 'the PR's tests failed'."""
    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kw):
        if args and args[0] == "docker":
            raise FileNotFoundError("docker")
        return await real(*args, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ws = workspace(tmp_path, "clean")
    with pytest.raises(SandboxUnavailable, match="not installed"):
        asyncio.run(Sandbox("docker", IMAGE, 30, False, "t6").run("test", ws))
