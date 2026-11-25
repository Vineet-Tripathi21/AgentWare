"""GitHub access: a real client (httpx + git) and a fixture-backed fake for mock mode.

Both expose the same async methods. Neither has a merge/approve/close/delete method:
those operations do not exist in the agent's code, by design.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SUMMARY_MARKER = "<!-- first-responder:summary -->"
API = "https://api.github.com"
BOT_NAME = "first-responder[bot]"
BOT_EMAIL = "first-responder@users.noreply.github.com"


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    head_ref: str
    head_sha: str
    base_ref: str
    author: str
    url: str = ""
    state: str = "open"


def _rmtree(path: Path) -> None:
    def _force(func, p, _exc):  # Windows: .git objects are read-only
        os.chmod(p, stat.S_IWRITE)
        func(p)

    # Cleanup must never raise: a leftover temp dir is harmless, a crashed worker is not.
    for attempt in range(3):
        if not path.exists():
            return
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_force)
            else:  # pragma: no cover
                shutil.rmtree(path, onerror=_force)
            return
        except OSError:
            time.sleep(0.5 * (attempt + 1))  # Windows: a killed process may still hold handles
    shutil.rmtree(path, ignore_errors=True)


def _parse_porcelain_z(out: str) -> list[str]:
    """`git status --porcelain -z`: robust to spaces/quotes; renames carry an extra field."""
    entries = out.split("\0")
    files, i = [], 0
    while i < len(entries):
        e = entries[i]
        if len(e) < 4:
            i += 1
            continue
        code, path = e[:2], e[3:]
        files.append(path)
        i += 2 if code[0] in "RC" else 1
    return sorted(set(files))


class GitHubError(Exception):
    pass


# ---------------------------------------------------------------- real


class GitHub:
    mode = "live"

    def __init__(self, token: str, repo: str, http: httpx.AsyncClient | None = None,
                 clone_base: str = "https://github.com", api_base: str = API) -> None:
        if not token or not repo or "/" not in repo:
            raise GitHubError("GITHUB_TOKEN and GITHUB_REPO (owner/name) are required in live mode")
        self.repo = repo
        self._token = token
        self.clone_url = f"{clone_base.rstrip('/')}/{repo}.git"
        self.web = "https://github.com"
        self.http = http or httpx.AsyncClient(
            base_url=api_base, timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "first-responder-agent"})
        self._no_hooks = Path(tempfile.gettempdir()) / "fr-no-git-hooks"  # never created: no hooks run

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = await self.http.request(method, url, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) or (
                    r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"):
                last = GitHubError(f"HTTP {r.status_code}")
                try:
                    wait = float(r.headers.get("retry-after", 2 ** attempt * 2))
                except ValueError:
                    wait = 2 ** attempt * 2
                await asyncio.sleep(min(wait, 30))
                continue
            if r.status_code >= 400:
                hint = ""
                if r.status_code in (401, 403):
                    hint = " (check GITHUB_TOKEN and its permissions; run `fr doctor`)"
                elif r.status_code == 404:
                    hint = " (not found: check GITHUB_REPO and that the token can see this repo)"
                raise GitHubError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:200]}{hint}")
            return r
        raise GitHubError(f"{method} {url} failed after retries: {last}")

    async def repo_info(self) -> dict[str, Any]:
        return (await self._req("GET", f"/repos/{self.repo}")).json()

    async def get_pr(self, number: int) -> PullRequest:
        d = (await self._req("GET", f"/repos/{self.repo}/pulls/{number}")).json()
        if d["head"]["repo"] is None or d["head"]["repo"]["full_name"] != self.repo:
            raise GitHubError("PRs from forks are out of scope (head is not in the sandbox repo)")
        return PullRequest(number=number, title=d.get("title") or "", body=d.get("body") or "",
                           head_ref=d["head"]["ref"], head_sha=d["head"]["sha"],
                           base_ref=d["base"]["ref"], author=(d.get("user") or {}).get("login", "?"),
                           url=d.get("html_url", ""), state=d.get("state", "open"))

    async def get_diff(self, number: int) -> str:
        r = await self._req("GET", f"/repos/{self.repo}/pulls/{number}",
                            headers={"Accept": "application/vnd.github.v3.diff"})
        return r.text

    def _git_env(self) -> dict[str, str]:
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        env = dict(os.environ)
        # Config via env vars: the token never appears in argv or .git/config, and the bot's
        # commits never run the developer's personal hooks or try to sign with their key.
        cfg = [("http.extraHeader", f"AUTHORIZATION: basic {basic}"),
               ("core.hooksPath", str(self._no_hooks)),
               ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
               ("user.name", BOT_NAME), ("user.email", BOT_EMAIL)]
        env["GIT_CONFIG_COUNT"] = str(len(cfg))
        for i, (k, v) in enumerate(cfg):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    async def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd) if cwd else None, env=self._git_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            raise
        text = out.decode("utf-8", "replace")
        if proc.returncode and check:
            raise GitHubError(f"git {args[0]} failed: {text[-300:]}")
        return text

    async def checkout(self, pr: PullRequest) -> Path:
        ws = Path(tempfile.mkdtemp(prefix=f"fr-pr{pr.number}-"))
        await self._git("clone", "--quiet", "--depth", "20", "--branch", pr.head_ref, self.clone_url, str(ws))
        head = (await self._git("rev-parse", "HEAD", cwd=ws)).strip()
        if head != pr.head_sha:
            raise GitHubError(f"PR head moved during the run ({pr.head_sha[:7]} -> {head[:7]}); "
                              "the next webhook will review the new head")
        return ws

    def cleanup(self, ws: Path | None) -> None:
        if ws:
            _rmtree(ws)

    async def reset_workspace(self, ws: Path) -> None:
        """Back to the exact PR head: drop anything the checks wrote."""
        await self._git("checkout", "--", ".", cwd=ws)
        await self._git("clean", "-fdxq", cwd=ws)

    async def changed_files(self, ws: Path) -> list[str]:
        return _parse_porcelain_z(await self._git("status", "--porcelain", "-z", "--untracked-files=all", cwd=ws))

    async def remote_branch_exists(self, ws: Path, branch: str) -> bool:
        out = await self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", cwd=ws)
        return bool(out.strip())

    async def push_branch(self, ws: Path, branch: str, message: str, files: list[str]) -> str:
        url = f"{self.web}/{self.repo}/tree/{branch}"
        if await self.remote_branch_exists(ws, branch):
            return url  # same PR head -> same fix; already pushed on an earlier run
        await self._git("checkout", "-q", "-b", branch, cwd=ws)
        await self._git("add", "--", *files, cwd=ws)  # exactly the files the policy approved
        await self._git("commit", "-q", "-m", message, cwd=ws)
        # Plain push of a brand-new ref. No --force, ever.
        await self._git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=ws)
        return url

    async def post_review(self, number: int, sha: str, body: str,
                          comments: list[dict[str, Any]]) -> str:
        payload = {"commit_id": sha, "event": "COMMENT", "body": body,
                   "comments": [{"path": c["path"], "line": c["line"], "side": "RIGHT",
                                 "body": c["body"]} for c in comments]}
        r = await self._req("POST", f"/repos/{self.repo}/pulls/{number}/reviews", json=payload)
        return r.json().get("html_url", "")

    async def upsert_summary(self, number: int, body: str) -> str:
        body = f"{SUMMARY_MARKER}\n{body}"
        r = await self._req("GET", f"/repos/{self.repo}/issues/{number}/comments",
                            params={"per_page": 100})
        for c in r.json():
            if SUMMARY_MARKER in (c.get("body") or ""):
                r2 = await self._req("PATCH", f"/repos/{self.repo}/issues/comments/{c['id']}",
                                     json={"body": body})
                return r2.json().get("html_url", "")
        r3 = await self._req("POST", f"/repos/{self.repo}/issues/{number}/comments",
                             json={"body": body})
        return r3.json().get("html_url", "")

    async def add_labels(self, number: int, labels: list[str]) -> None:
        await self._req("POST", f"/repos/{self.repo}/issues/{number}/labels",
                        json={"labels": labels})

    async def create_pull(self, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        """Used only by `fr seed` (a human-run setup command), never by the agent."""
        r = await self._req("POST", f"/repos/{self.repo}/pulls",
                            json={"title": title, "body": body, "head": head, "base": base})
        return r.json()


# ---------------------------------------------------------------- fake (mock mode)


@dataclass
class FakeGitHub:
    """Serves a PR from fixtures/prs/<name>/ and records every write instead of sending it.

    fixtures/base_repo/          the default branch
    fixtures/prs/<name>/pr.json  title, body, number, author
    fixtures/prs/<name>/overlay/ files added/changed by the PR
    fixtures/prs/<name>/delete.txt  paths the PR deletes (optional)
    """

    fixture: Path
    base_repo: Path
    repo: str = "sandbox/fr-sandbox"
    mode: str = "mock"
    actions: list[dict[str, Any]] = field(default_factory=list)
    delay_s: float = 0.0  # simulated API latency, makes the dashboard readable

    def __post_init__(self) -> None:
        self.meta = json.loads((self.fixture / "pr.json").read_text(encoding="utf-8"))
        self._snapshot: dict[str, bytes] = {}

    async def aclose(self) -> None:
        return None

    async def _lag(self) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)

    async def get_pr(self, number: int | None = None) -> PullRequest:
        await self._lag()
        m = self.meta
        return PullRequest(number=m.get("number", 1), title=m.get("title", ""),
                           body=m.get("body", ""), head_ref=m.get("head_ref", "feature"),
                           head_sha=m.get("head_sha", "0" * 40), base_ref="main",
                           author=m.get("author", "dev"),
                           url=f"mock://{self.repo}/pull/{m.get('number', 1)}",
                           state=m.get("state", "open"))

    def _deletes(self) -> list[str]:
        p = self.fixture / "delete.txt"
        return [ln.strip() for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    def _build_head(self, dest: Path) -> None:
        apply_fixture(self.base_repo, self.fixture, dest)

    async def get_diff(self, number: int | None = None) -> str:
        await self._lag()
        with tempfile.TemporaryDirectory(prefix="fr-diff-") as tmp:
            head = Path(tmp)
            self._build_head(head)
            return _tree_diff(self.base_repo, head)

    async def checkout(self, pr: PullRequest) -> Path:
        await self._lag()
        ws = Path(tempfile.mkdtemp(prefix=f"fr-pr{pr.number}-"))
        self._build_head(ws)
        self._snapshot = _read_tree(ws)
        return ws

    def cleanup(self, ws: Path | None) -> None:
        if ws:
            _rmtree(ws)

    async def reset_workspace(self, ws: Path) -> None:
        now = _read_tree(ws)
        for rel in set(now) - set(self._snapshot):
            (ws / rel).unlink(missing_ok=True)
        for rel, data in self._snapshot.items():
            if now.get(rel) != data:
                (ws / rel).parent.mkdir(parents=True, exist_ok=True)
                (ws / rel).write_bytes(data)

    async def changed_files(self, ws: Path) -> list[str]:
        now = _read_tree(ws)
        snap = self._snapshot
        return sorted(k for k in set(snap) | set(now) if snap.get(k) != now.get(k))

    async def push_branch(self, ws: Path, branch: str, message: str, files: list[str]) -> str:
        await self._lag()
        self.actions.append({"action": "push_branch", "branch": branch, "files": list(files),
                             "message": message})
        return f"mock://{self.repo}/tree/{branch}"

    async def post_review(self, number: int, sha: str, body: str,
                          comments: list[dict[str, Any]]) -> str:
        await self._lag()
        self.actions.append({"action": "post_review", "body": body, "comments": comments})
        return f"mock://{self.repo}/pull/{number}#review"

    async def upsert_summary(self, number: int, body: str) -> str:
        await self._lag()
        self.actions.append({"action": "upsert_summary", "body": body})
        return f"mock://{self.repo}/pull/{number}#summary"

    async def add_labels(self, number: int, labels: list[str]) -> None:
        await self._lag()
        self.actions.append({"action": "add_labels", "labels": labels})


def _read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def apply_fixture(base_repo: Path, fixture: Path, dest: Path) -> None:
    """Materialise a fixture PR's head tree: base repo + overlay - deletions."""
    shutil.copytree(base_repo, dest, dirs_exist_ok=True)
    overlay = fixture / "overlay"
    if overlay.exists():
        shutil.copytree(overlay, dest, dirs_exist_ok=True)
    deletes = fixture / "delete.txt"
    if deletes.exists():
        for rel in [ln.strip() for ln in deletes.read_text().splitlines() if ln.strip()]:
            (dest / rel).unlink(missing_ok=True)


def _tree_diff(base: Path, head: Path) -> str:
    def files(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}

    a, b = files(base), files(head)
    out: list[str] = []
    for rel in sorted(set(a) | set(b)):
        old = a[rel].read_text(encoding="utf-8", errors="replace").splitlines() if rel in a else []
        new = b[rel].read_text(encoding="utf-8", errors="replace").splitlines() if rel in b else []
        if old == new:
            continue
        out.append(f"diff --git a/{rel} b/{rel}")
        if rel not in a:
            out.append("new file mode 100644")
        if rel not in b:
            out.append("deleted file mode 100644")
        body = list(difflib.unified_diff(old, new, f"a/{rel}" if rel in a else "/dev/null",
                                         f"b/{rel}" if rel in b else "/dev/null", lineterm="", n=3))
        out.extend(body)
    return "\n".join(out) + "\n"
