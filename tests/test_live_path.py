"""The LIVE code path, end to end, without the internet.

Real `GitHub` client (httpx + git clone/commit/push) against:
  * a fake GitHub REST API (httpx.MockTransport) that behaves like the real endpoints, and
  * a local bare git repository standing in for github.com.
Only the network is fake. Policy, sandbox, git, API calls and payloads are the real ones.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import fr.runner
import fr.tools.github
from fr.config import ROOT
from fr.killswitch import KillSwitch
from fr.runner import Agent
from fr.seed import run_seed
from fr.store import Store
from fr.tools.github import GitHub

FIXTURES = ROOT / "fixtures"
REPO = "sandbox/fr-sandbox"


def git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", "-c", "user.name=dev", "-c", "user.email=dev@example.com",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/nonexistent", *args],
                       cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout


def overlay(fixture: str, dest: Path) -> None:
    fx = FIXTURES / "prs" / fixture
    if (fx / "overlay").exists():
        shutil.copytree(fx / "overlay", dest, dirs_exist_ok=True)
    if (fx / "delete.txt").exists():
        for rel in (fx / "delete.txt").read_text().split():
            (dest / rel).unlink()


class FakeGitHubAPI:
    """Just enough of api.github.com, backed by the bare repo for PR heads and diffs."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: dict[int, dict] = {}
        self.reviews: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.labels: dict[int, list[str]] = {}
        self.created_prs: list[dict] = []
        self.fail_next: list[int] = []          # status codes to return before succeeding
        self.reject_inline_comments = False
        self._id = 1000

    def add_pr(self, number: int, head: str, fixture: str) -> None:
        meta = json.loads((FIXTURES / "prs" / fixture / "pr.json").read_text())
        sha = git("rev-parse", head, cwd=self.bare).strip()
        self.prs[number] = {"number": number, "title": meta["title"], "body": meta["body"],
                            "state": "open", "html_url": f"https://github.com/{REPO}/pull/{number}",
                            "user": {"login": meta["author"]},
                            "head": {"ref": head, "sha": sha, "repo": {"full_name": REPO}},
                            "base": {"ref": "main"}}

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="upstream hiccup")
        path, m = req.url.path, req.method
        base = f"/repos/{REPO}"
        if m == "GET" and path == base:
            return httpx.Response(200, json={"full_name": REPO, "default_branch": "main",
                                             "private": True, "permissions": {"push": True}})
        if mm := re.fullmatch(rf"{base}/pulls/(\d+)", path):
            pr = self.prs[int(mm[1])]
            if "diff" in req.headers.get("accept", ""):
                return httpx.Response(200, text=git("diff", f"main...{pr['head']['ref']}", cwd=self.bare))
            return httpx.Response(200, json=pr)
        if m == "POST" and (mm := re.fullmatch(rf"{base}/pulls/(\d+)/reviews", path)):
            body = json.loads(req.content)
            if self.reject_inline_comments and body["comments"]:
                return httpx.Response(422, json={"message": "Unprocessable Entity: line must be part of the diff"})
            assert body["event"] == "COMMENT", "the agent must never approve or request changes"
            self.reviews.append({"pr": int(mm[1]), **body})
            return httpx.Response(200, json={"html_url": f"https://github.com/{REPO}/pull/{mm[1]}#review"})
        if mm := re.fullmatch(rf"{base}/issues/(\d+)/comments", path):
            n = int(mm[1])
            if m == "GET":
                return httpx.Response(200, json=self.comments.get(n, []))
            self._id += 1
            c = {"id": self._id, "body": json.loads(req.content)["body"], "html_url": f"c{self._id}"}
            self.comments.setdefault(n, []).append(c)
            return httpx.Response(201, json=c)
        if m == "PATCH" and (mm := re.fullmatch(rf"{base}/issues/comments/(\d+)", path)):
            for cs in self.comments.values():
                for c in cs:
                    if c["id"] == int(mm[1]):
                        c["body"] = json.loads(req.content)["body"]
                        return httpx.Response(200, json=c)
            return httpx.Response(404)
        if m == "POST" and (mm := re.fullmatch(rf"{base}/issues/(\d+)/labels", path)):
            self.labels.setdefault(int(mm[1]), []).extend(json.loads(req.content)["labels"])
            return httpx.Response(200, json=[])
        if m == "POST" and path == f"{base}/pulls":
            body = json.loads(req.content)
            self.created_prs.append(body)
            return httpx.Response(201, json={"number": 200 + len(self.created_prs),
                                             "html_url": f"https://github.com/{REPO}/pull/x"})
        return httpx.Response(404, json={"message": f"unhandled {m} {path}"})


@pytest.fixture
def world(tmp_path, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(fr.tools.github.asyncio, "sleep", no_sleep)  # retries without waiting
    # A dev repo with main = the sandbox app and two PR branches, pushed to a bare "github.com".
    work = tmp_path / "work"
    shutil.copytree(FIXTURES / "base_repo", work)
    git("init", "-q", "-b", "main", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "base", cwd=work)
    for branch, fixture in (("feature/mean", "lint"), ("perf/token", "risky"), ("feature/power", "clean")):
        git("checkout", "-q", "-b", branch, "main", cwd=work)
        overlay(fixture, work)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", fixture, cwd=work)
        git("checkout", "-q", "main", cwd=work)
    remotes = tmp_path / "remotes"
    bare = remotes / "sandbox" / "fr-sandbox.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    api = FakeGitHubAPI(bare)
    api.add_pr(102, "feature/mean", "lint")
    api.add_pr(103, "perf/token", "risky")
    api.add_pr(101, "feature/power", "clean")

    def make_gh() -> GitHub:
        http = httpx.AsyncClient(base_url="https://api.github.com",
                                 transport=httpx.MockTransport(api.handler))
        return GitHub("test-token", REPO, http=http, clone_base=remotes.as_posix())

    # Live mode runs PR code in Docker; here the local backend stands in (same Sandbox class).
    real_sandbox = fr.runner.Sandbox
    monkeypatch.setattr(fr.runner, "Sandbox",
                        lambda backend, image, timeout, mock, run_id: real_sandbox("local", image, timeout, True, run_id))
    return api, bare, make_gh


def live_agent(settings, gh, **kw):
    s = replace(settings, github_repo=REPO, **kw)
    return Agent(s, gh, KillSwitch(s.kill_file), mock_mode=False, bus=None, store=Store(s.state_dir))


def test_live_lint_pr_end_to_end(world, settings):
    api, bare, make_gh = world
    api.fail_next = [502, 503]  # GitHub hiccups twice; the client must retry and carry on
    gh = make_gh()
    res = asyncio.run(live_agent(settings, gh).run(102))
    asyncio.run(gh.aclose())
    assert res.status == "DONE", res.error
    assert "fr:autofix-pushed" in api.labels[102] and "risk:low" in api.labels[102]
    assert len(api.reviews) == 1 and api.reviews[0]["commit_id"] == api.prs[102]["head"]["sha"]
    assert all(c["side"] == "RIGHT" for c in api.reviews[0]["comments"])
    assert len(api.comments[102]) == 1 and "First Responder" in api.comments[102][0]["body"]
    # The fix branch exists on the remote, contains only the fixer's changes, by the bot.
    sha7 = api.prs[102]["head"]["sha"][:7]
    branch = f"agent/fix-102-{sha7}"
    assert branch in git("branch", "--list", "agent/*", cwd=bare)
    changed = git("diff", "--name-only", "feature/mean", branch, cwd=bare).split()
    assert changed and set(changed) <= {"calc/stats.py", "tests/test_stats.py"}
    assert git("log", "-1", "--format=%an", branch, cwd=bare).strip() == "first-responder[bot]"
    assert git("log", "--format=%s", "main", cwd=bare).strip() == "base", "main untouched"


def test_live_rerun_is_idempotent(world, settings):
    api, bare, make_gh = world
    for force in (False, False, True):
        gh = make_gh()
        res = asyncio.run(live_agent(settings, gh).run(102, force=force))
        asyncio.run(gh.aclose())
    assert res.status == "DONE"
    assert len(api.reviews) == 2, "the 2nd run was skipped (same head SHA), the forced 3rd ran"
    assert len(api.comments[102]) == 1, "summary comment is edited in place, never duplicated"
    assert git("branch", "--list", "agent/*", cwd=bare).count("agent/") == 1, "fix branch reused"


def test_live_risky_pr_never_pushes(world, settings):
    api, bare, make_gh = world
    gh = make_gh()
    res = asyncio.run(live_agent(settings, gh).run(103))
    asyncio.run(gh.aclose())
    assert res.status == "DONE" and res.risk == "high"
    assert "agent/" not in git("branch", "--list", "agent/*", cwd=bare)
    bodies = [c["body"].lower() for c in api.reviews[0]["comments"]]
    assert any("hardcoded credential" in b for b in bodies), bodies
    assert "risk:high" in api.labels[103]


def test_live_review_falls_back_when_inline_comments_are_rejected(world, settings):
    api, bare, make_gh = world
    api.reject_inline_comments = True
    gh = make_gh()
    res = asyncio.run(live_agent(settings, gh).run(103))
    asyncio.run(gh.aclose())
    assert res.status == "DONE"
    assert len(api.reviews) == 1 and api.reviews[0]["comments"] == []
    assert "calc/auth.py:" in api.reviews[0]["body"], "comments kept, folded into the body"


def test_live_github_down_fails_safe(world, settings):
    api, bare, make_gh = world
    api.fail_next = [503] * 4  # every retry fails on the very first call
    gh = make_gh()
    res = asyncio.run(live_agent(settings, gh).run(101))
    asyncio.run(gh.aclose())
    assert res.status == "FAILED_SAFE" and "failed after retries" in res.error


def test_live_closed_pr_is_skipped(world, settings):
    api, bare, make_gh = world
    api.prs[101]["state"] = "closed"
    gh = make_gh()
    res = asyncio.run(live_agent(settings, gh).run(101))
    asyncio.run(gh.aclose())
    assert res.status == "SKIPPED" and not api.reviews


def test_live_push_failure_does_not_lose_the_review(world, settings, monkeypatch):
    api, bare, make_gh = world
    gh = make_gh()

    async def broken_push(*a, **k):
        raise fr.tools.github.GitHubError("git push failed: remote rejected")
    monkeypatch.setattr(gh, "push_branch", broken_push)
    res = asyncio.run(live_agent(settings, gh).run(102))
    asyncio.run(gh.aclose())
    assert res.status == "DONE" and len(api.reviews) == 1
    assert "fr:autofix-pushed" not in api.labels[102]


def test_can_push_probe_writes_nothing(world, tmp_path):
    api, bare, make_gh = world
    before = git("for-each-ref", cwd=bare)
    gh = make_gh()
    ok, detail = asyncio.run(gh.can_push())
    asyncio.run(gh.aclose())
    assert ok, detail
    assert git("for-each-ref", cwd=bare) == before, "a dry-run probe must not create any ref"
    bad = GitHub("t", REPO, http=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
                 clone_base=(tmp_path / "does-not-exist").as_posix())
    ok, detail = asyncio.run(bad.can_push())
    asyncio.run(bad.aclose())
    assert not ok and "push" in detail


def test_seed_sets_up_an_empty_repo_and_opens_prs(tmp_path, settings, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(fr.tools.github.asyncio, "sleep", no_sleep)
    remotes = tmp_path / "remotes"
    bare = remotes / "sandbox" / "fr-sandbox.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    api = FakeGitHubAPI(bare)
    gh = GitHub("t", REPO, http=httpx.AsyncClient(base_url="https://api.github.com",
                                                   transport=httpx.MockTransport(api.handler)),
                clone_base=remotes.as_posix())
    rc = asyncio.run(run_seed(replace(settings, github_repo=REPO), ["clean", "hostile", "risky"], gh=gh))
    asyncio.run(gh.aclose())
    assert rc == 0
    assert ".first-responder.toml" in git("ls-tree", "--name-only", "main", cwd=bare)
    branches = git("branch", "--list", "demo/*", cwd=bare)
    assert branches.count("demo/") == 3
    assert [p["base"] for p in api.created_prs] == ["main"] * 3
    assert api.created_prs[1]["title"] == "Docs: add usage examples"
    risky = next(b.strip().lstrip("* ") for b in branches.splitlines() if "risky" in b)
    assert "tests/test_auth.py" in git("diff", "--name-only", "main", risky, cwd=bare)
