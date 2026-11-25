"""Settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'") and value.count(value[0]) >= 2:
            value = value[1: value.index(value[0], 1)]
        elif value.startswith("#"):
            value = ""
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if value:  # empty values in .env do not override defaults
            os.environ.setdefault(key.strip(), value)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    # LLM
    llm_provider: str = "mock"  # mock | gemini | groq | ollama
    llm_model: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    ollama_url: str = "http://localhost:11434"
    price_in_per_mtok: float = 0.0  # USD per 1M input tokens (0 on free tiers)
    price_out_per_mtok: float = 0.0

    # GitHub
    github_token: str = ""
    github_repo: str = ""  # owner/name: the ONLY repo the agent may touch
    webhook_secret: str = ""

    # Sandbox
    sandbox: str = "docker"  # live runs: docker (local is refused outside mock mode)
    mock_sandbox: str = "local"  # mock runs run our own fixture code, so local is fine
    sandbox_image: str = "fr-sandbox:latest"
    sandbox_timeout_s: int = 180

    # Budgets per run
    max_tool_calls: int = 40
    max_llm_calls: int = 6
    max_run_seconds: int = 420
    max_cost_usd: float = 0.50
    max_comments: int = 10

    # Paths
    traces_dir: Path = field(default_factory=lambda: ROOT / "traces")
    state_dir: Path = field(default_factory=lambda: ROOT / ".fr")

    @property
    def kill_file(self) -> Path:
        return self.state_dir / "KILL"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        e = os.environ.get
        provider = e("LLM_PROVIDER", "mock").lower()
        default_models = {
            # Checked against the providers' deprecation pages on 2026-11-26.
            "gemini": "gemini-3.8-flash",
            "groq": "openai/gpt-oss-120b",
            "ollama": "qwen2.5-coder:7b",
            "mock": "rules-v1",
        }
        s = cls(
            llm_provider=provider,
            llm_model=e("LLM_MODEL") or default_models.get(provider, ""),
            gemini_api_key=e("GEMINI_API_KEY", ""),
            groq_api_key=e("GROQ_API_KEY", ""),
            ollama_url=e("OLLAMA_URL", "http://localhost:11434"),
            price_in_per_mtok=_float("LLM_PRICE_IN_PER_MTOK", 0.0),
            price_out_per_mtok=_float("LLM_PRICE_OUT_PER_MTOK", 0.0),
            github_token=e("GITHUB_TOKEN", ""),
            github_repo=e("GITHUB_REPO", ""),
            webhook_secret=e("WEBHOOK_SECRET", ""),
            sandbox=e("SANDBOX", "docker").lower(),
            mock_sandbox=e("MOCK_SANDBOX", "local").lower(),
            sandbox_image=e("SANDBOX_IMAGE", "fr-sandbox:latest"),
            sandbox_timeout_s=_int("SANDBOX_TIMEOUT_S", 180),
            max_tool_calls=_int("MAX_TOOL_CALLS", 40),
            max_llm_calls=_int("MAX_LLM_CALLS", 6),
            max_run_seconds=_int("MAX_RUN_SECONDS", 420),
            max_cost_usd=_float("MAX_COST_USD", 0.50),
            max_comments=_int("MAX_COMMENTS", 10),
        )
        if e("FR_TRACES_DIR"):
            s.traces_dir = Path(e("FR_TRACES_DIR"))
        if e("FR_STATE_DIR"):
            s.state_dir = Path(e("FR_STATE_DIR"))
        return s
