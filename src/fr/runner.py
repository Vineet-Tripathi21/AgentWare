"""The agent loop: TRIGGER -> PLAN -> ACT -> CHECK -> REPORT.

The runner owns every side effect. The model only returns validated data; each
tool call is gated by the policy, checked against the kill switch, timed,
and written to the hash-chained audit trail before and after it runs.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import diffparse, injection, risk
from .audit import BUS, AuditLog, EventBus
from .config import Settings
from .killswitch import Killed, KillSwitch
from .llm import LLM, PLAN_SYSTEM, REVIEW_SYSTEM, LLMError, Plan, Review
from .policy import TOOLS, Budget, BudgetExceeded, Decision, Policy, redact
from .store import Store
from .tools.github import GitHubError, PullRequest
from .tools.sandbox import CheckResult, Sandbox, SandboxRefused

TERMINAL = {"DONE", "NEEDS_HUMAN", "FAILED_SAFE", "KILLED", "BUDGET_STOP", "SKIPPED"}


@dataclass
class RunResult:
    run_id: str
    status: str = "RUNNING"
    pr: int | None = None
    risk: str = "unknown"
    executed: list[str] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    injections: list[dict[str, str]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    trace: str = ""
    budget: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def new_run_id(pr: int | None) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-pr{pr if pr is not None else 'x'}-{uuid.uuid4().hex[:4]}"


class Agent:
    def __init__(self, settings: Settings, gh: Any, killswitch: KillSwitch, *,
                 mock_mode: bool, bus: EventBus | None = BUS, store: Store | None = None,
                 llm: LLM | None = None) -> None:
        self.s = settings
        self.gh = gh
        self.ks = killswitch
        self.mock = mock_mode
        self.bus = bus
        self.store = store
        self._own_llm = llm is None
        self.llm = llm or LLM(settings)
        # Mock runs execute our own fixture code; live runs execute PR code -> Docker only.
        self.backend = settings.mock_sandbox if mock_mode else settings.sandbox

    # ------------------------------------------------------------ helpers

    async def _tool(self, tool: str, args: dict[str, Any], fn: Callable[[], Awaitable[Any]],
                    summarize: Callable[[Any], Any] = lambda r: None, final: bool = False) -> Any:
        self.ks.check()
        d = self.policy.gate(tool, args) if not final else self._final_gate(tool, args)
        shown = {k: (v if not isinstance(v, (str, list)) or len(str(v)) < 300 else f"<{len(v)} items/chars>")
                 for k, v in args.items()}
        if not d.allowed:
            self.result.blocked.append({"tool": tool, "reason": d.reason})
            self.audit.log("policy_blocked", self.phase, tool=tool, tier=d.tier, reason=d.reason,
                           args=shown)
            return None
        self.audit.log("tool_call", self.phase, tool=tool, tier=d.tier, args=shown,
                       budget=self.budget.snapshot())
        t0 = time.monotonic()
        try:
            out = await fn()
        except asyncio.CancelledError:
            self.audit.log("tool_cancelled", self.phase, tool=tool,
                           ms=int((time.monotonic() - t0) * 1000))
            raise
        except Exception as e:
            self.audit.log("tool_error", self.phase, tool=tool, error=str(e)[:500],
                           ms=int((time.monotonic() - t0) * 1000))
            raise
        if d.tier == "write":
            self.result.executed.append(tool)
        self.audit.log("tool_result", self.phase, tool=tool, ok=True,
                       ms=int((time.monotonic() - t0) * 1000), result=summarize(out))
        return out

    def _final_gate(self, tool: str, args: dict[str, Any]) -> Decision:
        """The closing summary may run after a budget stop, but still passes validation."""
        spec = TOOLS[tool]
        err = spec.validate(args)
        return Decision(err is None, tool, spec.tier.value, err or "ok (final report)")

    def _phase(self, name: str) -> None:
        self.ks.check()
        self.phase = name
        self.audit.log("phase", name, budget=self.budget.snapshot())

    async def _ask(self, kind: str, system: str, user: str, ctx: dict[str, Any]):
        async def go():
            return await self.llm.complete(kind, system, user, ctx)
        out = await self._tool("llm", {"kind": kind, "model": self.llm.describe(),
                                       "prompt_chars": len(user)}, go,
                               lambda r: {"tokens_in": r[1].tokens_in, "tokens_out": r[1].tokens_out})
        model_out, usage = out
        self.budget.llm_calls += 1
        self.budget.tokens_in += usage.tokens_in
        self.budget.tokens_out += usage.tokens_out
        self.budget.cost_usd += self.llm.cost(usage)
        self.audit.log("model_output", self.phase, kind=kind, output=model_out.model_dump(),
                       cost_usd=round(self.budget.cost_usd, 6))
        return model_out

    # ------------------------------------------------------------ main

    async def run(self, pr_number: int | None = None, force: bool = False,
                  run_id: str | None = None) -> RunResult:
        self.run_id = run_id or new_run_id(pr_number)
        self.result = RunResult(run_id=self.run_id, pr=pr_number)
        self.audit = AuditLog(self.run_id, self.s.traces_dir, self.bus)
        self.result.trace = str(self.audit.path)
        self.budget = Budget.from_settings(self.s)
        self.policy = Policy(self.budget, plan_actions=set())  # nothing writable until a plan exists
        self.phase = "TRIGGER"
        self.pr: PullRequest | None = None
        self._ws: Path | None = None
        self.audit.log("run_started", "TRIGGER", pr=pr_number, repo=getattr(self.gh, "repo", ""),
                       mode=getattr(self.gh, "mode", "?"), llm=self.llm.describe(),
                       sandbox=self.backend, budgets={
                           "tool_calls": self.budget.max_tool_calls,
                           "llm_calls": self.budget.max_llm_calls,
                           "seconds": self.budget.max_seconds,
                           "cost_usd": self.budget.max_cost_usd})
        try:
            async with asyncio.timeout(self.s.max_run_seconds):
                await self._run(pr_number, force)
        except (asyncio.CancelledError, Killed) as e:
            self.result.status = "KILLED"
            self.result.error = str(e) or "kill switch"
            self.audit.log("run_killed", self.phase, reason=self.ks.reason or "kill switch",
                           note="pending tool calls were cancelled, not completed; no further writes")
        except (BudgetExceeded, TimeoutError) as e:
            self.result.status = "BUDGET_STOP"
            self.result.error = str(e) or f"run exceeded {self.s.max_run_seconds}s"
            self.audit.log("budget_stop", self.phase, reason=self.result.error)
            await self._final_report_on_error()
        except (LLMError, GitHubError, SandboxRefused) as e:
            self.result.status = "FAILED_SAFE"
            self.result.error = f"{type(e).__name__}: {e}"
            self.audit.log("failed_safe", self.phase, error=self.result.error)
            await self._final_report_on_error()
        except Exception as e:  # unexpected: stop cleanly, never continue blind
            self.result.status = "FAILED_SAFE"
            self.result.error = f"unexpected {type(e).__name__}: {e}"
            self.audit.log("failed_safe", self.phase, error=self.result.error)
            await self._final_report_on_error()
        finally:
            if self._ws is not None:
                try:
                    self.gh.cleanup(self._ws)
                except Exception as e:  # never let cleanup hide the run's real outcome
                    self.audit.log("cleanup_failed", self.phase, error=str(e)[:200])
            if self._own_llm:
                try:
                    await self.llm.aclose()
                except Exception:  # noqa: BLE001 - closing a client must not change the outcome
                    pass
        self.result.budget = self.budget.snapshot()
        self.audit.log("run_finished", self.phase, status=self.result.status,
                       risk=self.result.risk, executed=self.result.executed,
                       blocked=self.result.blocked, error=self.result.error,
                       budget=self.result.budget)
        if self.store and self.pr and self.result.status in {"DONE", "NEEDS_HUMAN"}:
            self.store.record(self._key, self.run_id, self.result.status)
        return self.result

    async def _run(self, pr_number: int | None, force: bool) -> None:
        # ---------------- TRIGGER: read the PR (all of it is untrusted data)
        self._phase("TRIGGER")
        pr: PullRequest = await self._tool("get_pr", {"pr": pr_number},
                                           lambda: self.gh.get_pr(pr_number),
                                           lambda p: {"title": p.title[:120], "head": p.head_ref,
                                                      "sha": p.head_sha[:7], "author": p.author,
                                                      "state": p.state})
        self.pr = pr
        self.result.pr = pr.number
        self._key = f"{getattr(self.gh, 'repo', '')}#{pr.number}@{pr.head_sha}"
        if pr.state != "open":
            self.result.status = "SKIPPED"
            self.audit.log("decision", "TRIGGER", what="skip", why=f"PR is {pr.state}, not open")
            return
        if pr.head_ref.startswith("agent/"):
            self.result.status = "SKIPPED"
            self.audit.log("decision", "TRIGGER", what="skip", why="PR head is an agent/* branch "
                           "(prevents the agent reviewing its own fixes in a loop)")
            return
        if self.store and not force and not self.mock and self.store.seen(self._key):
            self.result.status = "SKIPPED"
            self.audit.log("decision", "TRIGGER", what="skip",
                           why=f"already processed {self._key} (idempotency; use --force to re-run)")
            return

        diff_text = await self._tool("get_diff", {"pr": pr.number},
                                     lambda: self.gh.get_diff(pr.number),
                                     lambda d: {"bytes": len(d)})
        files = diffparse.parse_diff(diff_text)
        if not files:
            self.result.status = "DONE"
            self.result.risk = "low"
            self.audit.log("decision", "TRIGGER", what="stop", why="empty diff: nothing to review")
            return

        found = injection.scan(pr.title, "pr_title") + injection.scan(pr.body, "pr_body")
        for f in files:
            found += injection.scan("\n".join(f.added.values()), f"file:{f.path}")
        self.result.injections = [{"kind": i.kind, "source": i.source, "excerpt": redact(i.excerpt)}
                                  for i in found]
        for i in self.result.injections:
            self.audit.log("injection_detected", "TRIGGER", **i)
        findings = risk.assess(files)
        self.risk_findings = [f.__dict__ for f in findings]
        self.audit.log("risk_rules", "TRIGGER", findings=self.risk_findings[:40],
                       level=risk.max_level(*[f.level for f in findings]))

        # ---------------- PLAN: the model proposes, the policy disposes
        self._phase("PLAN")
        diff_view = diffparse.summarize(files)
        w_body, _ = injection.wrap_untrusted("pr_title_and_body", f"{pr.title}\n\n{pr.body}")
        w_diff, _ = injection.wrap_untrusted("diff", diff_view)
        ctx = {"files": [f.path for f in files], "risk_findings": self.risk_findings,
               "injections": self.result.injections, "pr_body": pr.body}
        user = (f"Repository: {getattr(self.gh, 'repo', '')}\nPR #{pr.number} by {pr.author}\n\n"
                f"{w_body}\n\n{w_diff}\n\nDeterministic risk findings (trusted):\n"
                f"{_bullets(self.risk_findings)}\n\nInjection scanner hits (trusted):\n"
                f"{_bullets(self.result.injections)}\n\nReturn the plan JSON.")
        plan: Plan = await self._ask("plan", PLAN_SYSTEM, user, ctx)

        rule_level = risk.max_level(*[f["level"] for f in self.risk_findings])
        level = risk.max_level(plan.risk, rule_level, "high" if found else "low")
        if level != plan.risk:
            self.audit.log("decision", "PLAN", what=f"risk raised {plan.risk} -> {level}",
                           why="rules/injection scanner can only raise the model's risk, never lower it")
        self.result.risk = level

        for act in plan.requested_actions:
            d = self.policy.gate(act, {})
            if not d.allowed:
                self.result.blocked.append({"tool": act, "reason": d.reason})
                self.audit.log("policy_blocked", "PLAN", tool=act, tier=d.tier, reason=d.reason,
                               source="model requested_actions")
            else:
                self.audit.log("decision", "PLAN", what=f"ignored requested action '{act}'",
                               why="requested_actions are advisory; only runner-defined steps execute")

        checks = list(dict.fromkeys(plan.checks))
        if "test" not in checks:
            checks.insert(0, "test")
            self.audit.log("decision", "PLAN", what="added 'test' to checks",
                           why="policy: tests always run, the plan cannot skip them")
        allowed = {"post_review", "upsert_summary", "add_labels"}
        autofix = plan.autofix and level != "high" and not found and not plan.needs_human
        if plan.autofix and not autofix:
            self.audit.log("decision", "PLAN", what="autofix disabled",
                           why="high risk, injection present, or a human question is open: "
                               "no code changes for this PR")
        if autofix:
            allowed.add("push_branch")
        self.policy.plan_actions = allowed
        question = redact(plan.question or "") if plan.needs_human else ""
        if plan.needs_human:
            self.audit.log("decision", "PLAN", what="ask a human",
                           why="the model found the PR ambiguous; running the safe checks only, "
                               "making no code changes")
        self.audit.log("plan_accepted", "PLAN", risk=level, checks=checks, autofix=autofix,
                       writable_tools=sorted(allowed), needs_human=plan.needs_human)

        # ---------------- ACT: checks in the sandbox, then review
        self._phase("ACT")
        sandbox = Sandbox(self.backend, self.s.sandbox_image, self.s.sandbox_timeout_s,
                          self.mock, self.run_id)
        ws = await self._tool("checkout", {"ref": pr.head_ref}, lambda: self.gh.checkout(pr),
                              lambda p: {"workspace": Path(p).name})
        self._ws = ws
        results: dict[str, CheckResult] = {}
        for c in checks:
            res = await self._tool("run_sandbox", {"command": c, "backend": self.backend},
                                   lambda c=c: sandbox.run(c, ws), lambda r: r.brief())
            results[c] = res
        self.result.checks = {k: v.brief() for k, v in results.items()}
        for k, v in results.items():
            for i in injection.scan(v.output, f"{k}_output"):
                hit = {"kind": i.kind, "source": i.source, "excerpt": redact(i.excerpt)}
                self.result.injections.append(hit)
                self.audit.log("injection_detected", "ACT", **hit)

        lint_issues = results["lint"].lint_issues if "lint" in results else []
        w_checks, _ = injection.wrap_untrusted(
            "check_output", "\n\n".join(f"[{k}] exit={v.exit_code}\n{v.output[-2500:]}"
                                        for k, v in results.items()))
        rctx = {**ctx, "lint_issues": lint_issues,
                "checks": {k: v.brief() for k, v in results.items()}}
        ruser = (f"PR #{pr.number}. Plan summary (model): {plan.summary}\nRisk: {level}\n\n"
                 f"{w_diff}\n\n{w_checks}\n\nRisk findings (trusted):\n{_bullets(self.risk_findings)}"
                 f"\n\nReturn the review JSON.")
        review: Review = await self._ask("review", REVIEW_SYSTEM, ruser, rctx)

        valid_lines = {f.path: set(f.added) for f in files}
        comments, dropped = [], 0
        for c in review.comments:
            if c.line in valid_lines.get(c.path, set()) and len(comments) < self.s.max_comments:
                comments.append({**c.model_dump(), "body": redact(c.body)})
            else:
                dropped += 1
        if dropped:
            self.audit.log("decision", "ACT", what=f"dropped {dropped} model comment(s)",
                           why="not on an added line of this diff, or over the comment cap")

        # ---------------- CHECK: autofix only if it keeps tests green
        self._phase("CHECK")
        fix_url = await self._autofix(pr, sandbox, ws, lint_issues) if autofix and lint_issues else None
        if not (autofix and lint_issues):
            self.audit.log("decision", "CHECK", what="no autofix",
                           why="not eligible" if not autofix else "no lint issues")

        # ---------------- REPORT
        self._phase("REPORT")
        test = results["test"]
        labels = [f"risk:{level}", "fr:reviewed"]
        if test.note == "no tests were collected":
            labels.append("fr:no-tests")
        elif not test.ok:
            labels.append("fr:tests-failing")
        if self.result.injections:
            labels.append("fr:injection-detected")
        if fix_url:
            labels.append("fr:autofix-pushed")
        if question:
            labels.append("fr:needs-human")
        await self._tool("add_labels", {"labels": labels},
                         lambda: self.gh.add_labels(pr.number, labels))
        verdict = redact(review.verdict) or "No review notes."
        review_body = f"**First Responder review** ({level} risk)\n\n{verdict}"
        await self._post_review(pr, review_body, comments)
        summary = redact(self._summary_md(level, results, review, comments, fix_url, question))
        await self._tool("upsert_summary", {"body": summary},
                         lambda: self.gh.upsert_summary(pr.number, summary),
                         lambda u: {"url": u})
        self.result.status = "NEEDS_HUMAN" if question else "DONE"

    async def _autofix(self, pr: PullRequest, sandbox: Sandbox, ws: Path,
                       lint_issues: list[dict]) -> str | None:
        # Start from the exact PR head, so the commit contains the fixer's changes and nothing else.
        await self.gh.reset_workspace(ws)
        fix = await self._tool("run_sandbox", {"command": "fix", "backend": self.backend},
                               lambda: sandbox.run("fix", ws), lambda r: r.brief())
        if fix is None:
            return None
        changed = await self.gh.changed_files(ws)
        if not changed:
            self.audit.log("decision", "CHECK", what="no autofix", why="the fixer changed nothing")
            return None
        retest = await self._tool("run_sandbox", {"command": "test", "backend": self.backend},
                                  lambda: sandbox.run("test", ws), lambda r: r.brief())
        if retest is None or not retest.ok:
            self.audit.log("decision", "CHECK", what="autofix discarded",
                           why="tests did not pass after the fix; nothing pushed")
            return None
        branch = f"agent/fix-{pr.number}-{pr.head_sha[:7]}"
        try:
            return await self._tool(
                "push_branch", {"branch": branch, "files": changed},
                lambda: self.gh.push_branch(ws, branch,
                                            f"style: autofix lint for #{pr.number} (First Responder)",
                                            changed),
                lambda u: {"url": u, "files": changed})
        except GitHubError as e:
            # The fix is optional; the review is the job. Report the failure and carry on.
            self.audit.log("decision", "CHECK", what="autofix push failed, continuing with the review",
                           why=str(e)[:300])
            return None

    async def _post_review(self, pr: PullRequest, body: str, comments: list[dict]) -> None:
        self.budget.comments += len(comments)
        try:
            await self._tool("post_review", {"body": body, "n_comments": len(comments)},
                             lambda: self.gh.post_review(pr.number, pr.head_sha, body, comments),
                             lambda u: {"url": u, "comments": len(comments)})
        except GitHubError as e:
            if not comments or "422" not in str(e):
                raise
            # GitHub refused the inline comments (e.g. the PR moved on). Keep them, inline in the body.
            self.audit.log("decision", "REPORT", what="inline comments rejected; posting them in the body",
                           why=str(e)[:200])
            folded = body + "\n\n" + "\n".join(
                f"- `{c['path']}:{c['line']}` ({c['severity']}): {c['body']}" for c in comments)
            await self._tool("post_review", {"body": folded, "n_comments": 0},
                             lambda: self.gh.post_review(pr.number, pr.head_sha, folded, []),
                             lambda u: {"url": u, "comments": 0})

    # ------------------------------------------------------------ reporting

    def _summary_md(self, level: str, results: dict[str, CheckResult], review: Review,
                    comments: list[dict], fix_url: str | None, question: str) -> str:
        icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}[level]

        def status(v: CheckResult) -> str:
            if v.ok:
                return "✅ pass"
            if v.timed_out:
                return "⏱ timeout"
            return f"⚠️ {v.note}" if v.note else "❌ fail"
        rows = "\n".join(f"| `{k}` | {status(v)} | {v.duration_s}s |" for k, v in results.items())
        parts = [f"## {icon} First Responder: {level} risk", ""]
        if question:
            parts += [f"> **🛑 Needs a human:** {question}", ">",
                      "> I ran the safe checks below but made no code changes.", ""]
        parts += ["| check | result | time |", "|---|---|---|", rows, "",
                  f"**Verdict:** {review.verdict or 'No review notes.'}"]
        if review.ci_diagnosis:
            parts += ["", f"**Failure diagnosis:** {review.ci_diagnosis}"]
        parts += ["", "**What I did**",
                  f"- ran {', '.join(results)} in an isolated sandbox",
                  f"- left {len(comments)} line comment(s)"]
        if fix_url:
            parts.append(f"- pushed mechanical lint fixes to [{fix_url.rsplit('/', 1)[-1]}]({fix_url}) "
                         "(tests pass on it). Merge it yourself if you want it.")
        if self.result.blocked:
            parts += ["", "**What I refused to do**"]
            parts += [f"- `{b['tool']}`: {b['reason']}" for b in self.result.blocked]
        if self.result.injections:
            parts += ["", "**⚠️ Instruction-like text found in untrusted content (ignored)**"]
            parts += [f"- `{i['kind']}` in {i['source']}: \"{i['excerpt'][:90]}\""
                      for i in self.result.injections[:6]]
        b = self.budget.snapshot()
        parts += ["", f"<sub>run `{self.run_id}` · model `{self.llm.describe()}` · "
                      f"{b['llm_calls']} model calls · {b['tokens_in']}+{b['tokens_out']} tokens · "
                      f"${b['cost_usd']:.4f} · {b['elapsed_s']}s · I never merge, approve, or push to "
                      f"the default branch.</sub>"]
        return "\n".join(parts)

    async def _final_report_on_error(self) -> None:
        if self.pr is None or self.ks.engaged:
            return
        body = redact(f"### ⚠️ First Responder stopped early\n\nStatus: `{self.result.status}`. "
                      f"Reason: {self.result.error}\n\nI stopped instead of guessing. Nothing was merged "
                      f"or pushed to a protected branch. Trace: `{self.run_id}`")
        try:
            await self._tool("upsert_summary", {"body": body},
                             lambda: self.gh.upsert_summary(self.pr.number, body), final=True)
        except Exception as e:  # reporting must never mask the original failure
            self.audit.log("report_failed", self.phase, error=str(e)[:300])


def _bullets(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- none"
    return "\n".join("- " + ", ".join(f"{k}={v}" for k, v in it.items() if v is not None)
                     for it in items[:30])
