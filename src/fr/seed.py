"""`fr seed`: set up a live sandbox repo for a demo or a judge, in one command.

1. If the repo is empty, push fixtures/base_repo as its default branch.
   (An existing default branch is never touched.)
2. For each chosen fixture, push a `demo/<fixture>-<time>` branch with the PR's
   changes and open a pull request. If the webhook is set up, the agent picks
   each PR up on its own.

This is a human-run setup tool, not part of the agent: the agent has no code
path that creates branches outside agent/* or opens pull requests.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from .config import ROOT, Settings
from .tools.github import GitHub, GitHubError, _rmtree

FIXTURES = ROOT / "fixtures"


def _apply_overlay(fixture: Path, dest: Path) -> None:
    overlay = fixture / "overlay"
    if overlay.exists():
        shutil.copytree(overlay, dest, dirs_exist_ok=True)
    deletes = fixture / "delete.txt"
    if deletes.exists():
        for rel in [ln.strip() for ln in deletes.read_text().splitlines() if ln.strip()]:
            (dest / rel).unlink(missing_ok=True)


async def run_seed(s: Settings, names: list[str], gh: GitHub | None = None) -> int:
    own = gh is None
    gh = gh or GitHub(s.github_token, s.github_repo)
    ws = Path(tempfile.mkdtemp(prefix="fr-seed-"))
    try:
        info = await gh.repo_info()
        default = info.get("default_branch") or "main"
        await gh._git("clone", "--quiet", gh.clone_url, str(ws))
        has_default = bool((await gh._git("ls-remote", "--heads", "origin", f"refs/heads/{default}",
                                          cwd=ws)).strip())
        if not has_default:
            print(f"repo is empty: pushing the sandbox app as '{default}'")
            shutil.copytree(FIXTURES / "base_repo", ws, dirs_exist_ok=True)
            await gh._git("checkout", "-q", "-B", default, cwd=ws)
            await gh._git("add", "-A", cwd=ws)
            await gh._git("commit", "-q", "-m", "Sandbox app for First Responder demos", cwd=ws)
            await gh._git("push", "-q", "origin", f"HEAD:refs/heads/{default}", cwd=ws)
        else:
            print(f"'{default}' already exists: leaving it untouched")
        await gh._git("fetch", "-q", "origin", f"+refs/heads/{default}:refs/remotes/origin/{default}", cwd=ws)
        for name in names:
            fx = FIXTURES / "prs" / name
            if not fx.is_dir():
                print(f"  skip: unknown fixture '{name}'")
                continue
            meta = json.loads((fx / "pr.json").read_text(encoding="utf-8"))
            branch = f"demo/{name}-{int(time.time())}"
            await gh._git("checkout", "-q", "-B", branch, f"origin/{default}", cwd=ws)
            _apply_overlay(fx, ws)
            await gh._git("add", "-A", cwd=ws)
            await gh._git("commit", "-q", "-m", meta.get("title", name), cwd=ws)
            await gh._git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=ws)
            pr = await gh.create_pull(meta.get("title", name), meta.get("body", ""), branch, default)
            print(f"  opened PR #{pr.get('number')}: {meta.get('title')}  {pr.get('html_url', '')}")
        return 0
    except GitHubError as e:
        print(f"seed failed: {e}")
        return 1
    finally:
        _rmtree(ws)
        if own:
            await gh.aclose()
