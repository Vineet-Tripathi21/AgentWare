"""One-line, human-readable rendering of audit events (CLI live view and replay)."""

from __future__ import annotations

import time
from typing import Any

C = {"reset": "\033[0m", "dim": "\033[2m", "red": "\033[31m", "green": "\033[32m",
     "yellow": "\033[33m", "blue": "\033[34m", "mag": "\033[35m", "cyan": "\033[36m",
     "bold": "\033[1m"}


def color(text: str, c: str, on: bool = True) -> str:
    return f"{C[c]}{text}{C['reset']}" if on else text


def format_event(ev: dict[str, Any], use_color: bool = True) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0)))
    ph = (ev.get("phase") or "").ljust(7)
    typ = ev.get("type", "")
    d = ev.get("data", {})
    k = lambda s, c: color(s, c, use_color)
    if typ == "run_started":
        msg = k(f"▶ run {ev['run_id']} started", "bold") + \
            f"  pr={d.get('pr')} mode={d.get('mode')} llm={d.get('llm')} sandbox={d.get('sandbox')}"
    elif typ == "phase":
        msg = k(f"── {ev.get('phase')} ──", "cyan")
    elif typ == "tool_call":
        tier = d.get("tier")
        msg = k(f"→ {d.get('tool')}", "blue" if tier == "read" else "mag") + f" [{tier}] {_short(d.get('args'))}"
    elif typ == "tool_result":
        msg = k(f"✓ {d.get('tool')}", "green") + f" {d.get('ms')}ms {_short(d.get('result'))}"
    elif typ == "tool_error":
        msg = k(f"✗ {d.get('tool')} error: {d.get('error')}", "red")
    elif typ == "tool_cancelled":
        msg = k(f"■ {d.get('tool')} CANCELLED after {d.get('ms')}ms", "red")
    elif typ == "policy_blocked":
        msg = k(f"⛔ BLOCKED {d.get('tool')}", "red") + f" ({d.get('reason')})"
    elif typ == "injection_detected":
        msg = k(f"⚠ injection [{d.get('kind')}] in {d.get('source')}", "yellow") + \
            f": \"{(d.get('excerpt') or '')[:70]}\""
    elif typ == "decision":
        msg = k(f"◆ {d.get('what')}", "yellow") + k(f"  because {d.get('why')}", "dim")
    elif typ == "plan_accepted":
        msg = k("◆ plan accepted", "bold") + f" risk={d.get('risk')} checks={d.get('checks')} " \
            f"autofix={d.get('autofix')} writable={d.get('writable_tools')}"
    elif typ == "model_output":
        out = d.get("output", {})
        msg = k(f"✎ model {d.get('kind')}", "cyan") + " " + _short(
            {kk: out.get(kk) for kk in ("risk", "requested_actions", "verdict", "ci_diagnosis") if kk in out})
    elif typ == "risk_rules":
        msg = f"⚑ rules: level={d.get('level')} findings={len(d.get('findings', []))}"
    elif typ == "run_killed":
        msg = k(f"■■ KILLED: {d.get('reason')}. {d.get('note')}", "red")
    elif typ in ("failed_safe", "budget_stop"):
        msg = k(f"⚠ {typ.upper()}: {d.get('error') or d.get('reason')}", "red")
    elif typ == "run_finished":
        st = d.get("status")
        msg = k(f"■ run finished: {st}", "green" if st in ("DONE", "SKIPPED") else "red") + \
            f" risk={d.get('risk')} wrote={d.get('executed')} blocked={len(d.get('blocked', []))} " \
            f"budget={_short(d.get('budget'))}"
    else:
        msg = f"{typ} {_short(d)}"
    return f"{k(t, 'dim')} {k(ph, 'dim')} {msg}"


def _short(v: Any, n: int = 140) -> str:
    s = str(v) if v is not None else ""
    return s if len(s) <= n else s[: n - 1] + "…"
