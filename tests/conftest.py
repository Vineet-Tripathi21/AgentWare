from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from fr.config import ROOT, Settings
from fr.killswitch import KillSwitch
from fr.runner import Agent
from fr.tools.github import FakeGitHub

FIXTURES = ROOT / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    return replace(s, llm_provider="mock", llm_model="rules-v1", sandbox="local",
                   traces_dir=tmp_path / "traces", state_dir=tmp_path / "state",
                   sandbox_timeout_s=60)


@pytest.fixture
def make_agent(settings: Settings):
    def _make(fixture: str, **overrides):
        s = replace(settings, **overrides)
        gh = FakeGitHub(fixture=FIXTURES / "prs" / fixture, base_repo=FIXTURES / "base_repo")
        ks = KillSwitch(s.kill_file)
        return Agent(s, gh, ks, mock_mode=True, bus=None), gh, ks
    return _make
