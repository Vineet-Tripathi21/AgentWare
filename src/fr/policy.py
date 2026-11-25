"""The policy engine: every tool call goes through `Policy.gate()` before it runs.

This is enforced in code, not in the prompt. The model never calls tools
directly; it only returns data. The runner turns that data into tool calls, and
each one must pass this gate.

Tiers
  READ      : no side effects (fetch diff, run checks in the sandbox)
  WRITE     : reversible, scoped side effects (comment, label, push to agent/*)
  FORBIDDEN : irreversible or high blast radius. Always blocked.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .config import Settings


class Tier(StrEnum):
    READ = "read"
    WRITE = "write"
    FORBIDDEN = "forbidden"


AGENT_BRANCH = re.compile(r"^agent/[a-z0-9][a-z0-9._/-]{0,80}$")
ALLOWED_CHECKS = {"test", "lint"}
ALLOWED_SANDBOX_COMMANDS = {"test", "lint", "fix"}
ALLOWED_LABELS = re.compile(r"^(fr:[a-z0-9-]+|risk:(low|medium|high))$")
PROTECTED_PATHS = re.compile(r"^(\.github/|\.first-responder\.toml$)")


@dataclass
class ToolSpec:
    name: str
    tier: Tier
    validate: Callable[[dict[str, Any]], str | None] = lambda a: None  # returns error or None
    description: str = ""


def _v_branch(args: dict[str, Any]) -> str | None:
    br = str(args.get("branch", ""))
    if not AGENT_BRANCH.match(br) or ".." in br or "//" in br or br.endswith((".lock", "/")):
        return f"branch '{br}' is outside the agent/* namespace"
    files = args.get("files", [])
    bad = [f for f in files if PROTECTED_PATHS.match(f)]
    if bad:
        return f"refusing to modify protected paths: {bad}"
    return None


def _v_sandbox(args: dict[str, Any]) -> str | None:
    cmd = args.get("command")
    if cmd not in ALLOWED_SANDBOX_COMMANDS:
        return f"sandbox command '{cmd}' is not in the allowlist {sorted(ALLOWED_SANDBOX_COMMANDS)}"
    return None


def _v_labels(args: dict[str, Any]) -> str | None:
    bad = [lb for lb in args.get("labels", []) if not ALLOWED_LABELS.match(lb)]
    return f"labels not allowed: {bad}" if bad else None


SECRET_PATTERNS = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_\-]{30,}"
    r"|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_\-]{20,}|gsk_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$))")


def redact(text: str) -> str:
    """Replace anything secret-shaped before it is posted to GitHub or written to a trace."""
    return SECRET_PATTERNS.sub("[REDACTED]", text) if text else text


def _v_comment(args: dict[str, Any]) -> str | None:
    body = str(args.get("body", ""))
    if len(body) > 60000:
        return "comment too long"
    if SECRET_PATTERNS.search(body):
        return "comment appears to contain a secret"
    return None


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec("get_pr", Tier.READ, description="Read PR metadata"),
        ToolSpec("get_diff", Tier.READ, description="Read the PR diff"),
        ToolSpec("checkout", Tier.READ, description="Clone the PR head into a temp workspace"),
        ToolSpec("run_sandbox", Tier.READ, _v_sandbox, "Run an allowlisted check in the sandbox"),
        ToolSpec("llm", Tier.READ, description="Ask the model (returns data only)"),
        ToolSpec("post_review", Tier.WRITE, _v_comment, "Post a COMMENT review with line notes"),
        ToolSpec("upsert_summary", Tier.WRITE, _v_comment, "Create/update the summary comment"),
        ToolSpec("add_labels", Tier.WRITE, _v_labels, "Add fr:* / risk:* labels"),
        ToolSpec("push_branch", Tier.WRITE, _v_branch, "Push a fix commit to agent/*"),
        # Forbidden: the agent has no code path that performs these. They exist so that
        # a request for them (from the model or from injected text) is logged and refused.
        ToolSpec("merge_pr", Tier.FORBIDDEN, description="Merge a PR"),
        ToolSpec("approve_pr", Tier.FORBIDDEN, description="Approve a PR"),
        ToolSpec("close_pr", Tier.FORBIDDEN, description="Close a PR"),
        ToolSpec("force_push", Tier.FORBIDDEN, description="Force-push any branch"),
        ToolSpec("push_default_branch", Tier.FORBIDDEN, description="Push to main/default"),
        ToolSpec("delete_branch", Tier.FORBIDDEN, description="Delete a branch"),
        ToolSpec("edit_workflows", Tier.FORBIDDEN, description="Modify .github/workflows"),
        ToolSpec("read_secrets", Tier.FORBIDDEN, description="Read env vars / secrets"),
        ToolSpec("run_shell", Tier.FORBIDDEN, description="Run arbitrary shell commands"),
        ToolSpec("deploy", Tier.FORBIDDEN, description="Deploy anything"),
    ]
}


@dataclass
class Decision:
    allowed: bool
    tool: str
    tier: str
    reason: str


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    max_tool_calls: int
    max_llm_calls: int
    max_seconds: int
    max_cost_usd: float
    max_comments: int
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    comments: int = 0

    @classmethod
    def from_settings(cls, s: Settings) -> Budget:
        return cls(s.max_tool_calls, s.max_llm_calls, s.max_run_seconds, s.max_cost_usd,
                   s.max_comments)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self) -> dict[str, Any]:
        return {
            "tool_calls": self.tool_calls, "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6), "elapsed_s": round(self.elapsed, 1),
        }


class Policy:
    def __init__(self, budget: Budget, plan_actions: set[str] | None = None) -> None:
        self.budget = budget
        # Tools the validated plan made eligible. READ tools are always eligible.
        self.plan_actions = plan_actions

    def gate(self, tool: str, args: dict[str, Any] | None = None) -> Decision:
        args = args or {}
        spec = TOOLS.get(tool)
        if spec is None:
            return Decision(False, tool, "unknown", "unknown tool: not in the registry")
        if spec.tier is Tier.FORBIDDEN:
            return Decision(False, tool, spec.tier.value, "forbidden by policy (irreversible)")
        if spec.tier is Tier.WRITE and self.plan_actions is not None \
                and tool not in self.plan_actions:
            return Decision(False, tool, spec.tier.value, "not part of the validated plan")
        err = spec.validate(args)
        if err:
            return Decision(False, tool, spec.tier.value, err)
        b = self.budget
        if b.tool_calls >= b.max_tool_calls:
            raise BudgetExceeded(f"tool call budget ({b.max_tool_calls}) exhausted")
        if b.elapsed > b.max_seconds:
            raise BudgetExceeded(f"time budget ({b.max_seconds}s) exhausted")
        if tool == "llm":
            if b.llm_calls >= b.max_llm_calls:
                raise BudgetExceeded(f"LLM call budget ({b.max_llm_calls}) exhausted")
            if b.cost_usd >= b.max_cost_usd:
                raise BudgetExceeded(f"cost budget (${b.max_cost_usd}) exhausted")
        b.tool_calls += 1
        return Decision(True, tool, spec.tier.value, "ok")
