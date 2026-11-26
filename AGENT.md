# AGENT.md

**Track:** 5, Developer Workflow
**Job:** do the first pass on a pull request (tests, lint, risk, review, safe autofix), then stop.
**Trigger:** GitHub `pull_request` webhook (`opened`, `synchronize`, `reopened`, `ready_for_review`), or `fr run --pr N`.

## Stack

| piece | choice |
|---|---|
| language | Python 3.11+ (asyncio, so every step can be cancelled) |
| model | provider-agnostic. `gemini` (default model `gemini-3.8-flash`, free tier), `groq` (`openai/gpt-oss-120b`, free tier), `ollama` (`qwen2.5-coder:7b`, local), `mock` (deterministic rules, no key). Set with `LLM_PROVIDER` / `LLM_MODEL`. Defaults checked against the providers' deprecation pages on 26 Nov 2026; `fr doctor` verifies the key and model. |
| framework | none. A plain, explicit loop, so every step is visible in the trace |
| web | FastAPI + Server-Sent Events, with a single static HTML dashboard |
| GitHub | REST API via httpx; `git` for clone and push (token passed via `GIT_CONFIG_*` env, never in argv or `.git/config`) |
| sandbox | Docker: `--network none`, `--pull never`, 768 MB, 1 CPU, 256 pids, `--cap-drop ALL`, `no-new-privileges`, read-only root, runs as your uid on Linux/macOS |
| state | SQLite (idempotency), JSONL traces |
| AI coding tools | Claude Code was used to help write this project |

## Architecture

```
webhook.py/server.py ──▶ queue ──▶ worker ──▶ Agent.run()  (asyncio Task, registered with KillSwitch)
                                                │
       ┌────────────────────────────────────────┼─────────────────────────────────────────┐
       │ TRIGGER  get_pr, get_diff (READ)        │ every tool call:                         │
       │          injection.scan, risk.assess    │   killswitch.check()                     │
       │ PLAN     llm(plan) → Plan (pydantic)    │   policy.gate(tool, args) → allow/block   │
       │          policy decides writable tools  │   audit.log(tool_call)                   │
       │ ACT      checkout, run_sandbox(test,lint)│   await tool()   ← cancellable           │
       │          llm(review) → Review           │   audit.log(tool_result | error | cancel)│
       │ CHECK    run_sandbox(fix) → retest → push│                                          │
       │ REPORT   add_labels, post_review, summary│                                          │
       └────────────────────────────────────────┴─────────────────────────────────────────┘
```

**Separation of thinking and doing.** The model returns JSON only (`Plan`, `Review` schemas). It cannot name a shell command, a branch, or a tool to call. The runner has a fixed sequence of steps. The model's output can only:
- raise the risk level (rules and the injection scanner can also raise it; nothing can lower it below the rules),
- pick checks from `{test, lint}` (tests are always added back if dropped),
- ask for autofix (granted only for non-high-risk PRs with no injection),
- suggest `requested_actions`, which are logged and almost always blocked,
- write review text and line comments (validated against the diff).

## Tools and permissions

| tool | tier | guard |
|---|---|---|
| `get_pr`, `get_diff`, `checkout` | READ | sandbox repo only; fork PRs refused |
| `run_sandbox` | READ | command **key** must be `test`/`lint`/`fix`; the command line comes from the repo's `.first-responder.toml` |
| `llm` | READ | call, token and cost budgets |
| `post_review` | WRITE | only if in plan; event is always `COMMENT`; comments must sit on added lines; secret-shaped text refused |
| `upsert_summary` | WRITE | one comment per PR (hidden marker), edited in place |
| `add_labels` | WRITE | only `risk:*` / `fr:*` labels |
| `push_branch` | WRITE | branch must match `^agent/…`; no `..`; may not touch `.github/` or `.first-responder.toml`; only after tests pass on the fix; no `--force` |
| `merge_pr`, `approve_pr`, `close_pr`, `force_push`, `push_default_branch`, `delete_branch`, `edit_workflows`, `read_secrets`, `run_shell`, `deploy` | FORBIDDEN | always blocked; **no implementation exists** |

GitHub token scope needed: one repository; Contents R/W, Pull requests R/W, Issues R/W, Metadata R.

## Failure handling

| failure | behaviour |
|---|---|
| GitHub 429/5xx/network | 4 tries with backoff, honouring `Retry-After`, then `FAILED_SAFE` + summary comment |
| model overloaded (429/5xx), timed out, or retired (404) | retries with backoff, then **falls back** to the next free model (`gemini-3.6-flash` → `gemini-3.5-flash-lite`), remembered for 10 min; the trace and PR summary say which model answered |
| every model down | the step is done by **deterministic rules** instead; PR labelled `fr:rules-only` and the summary says so. Tests, lint, risk rules and the injection scanner still run |
| bad model key / refused request | config error: `FAILED_SAFE` + summary comment (needs a human) |
| model returns messy JSON | normalised (synonyms, truncation, bad comments dropped one by one); if still invalid, one repair attempt, then `FAILED_SAFE` |
| model key wrong / model retired | clear error naming the fix (`fr doctor`), `FAILED_SAFE` + summary comment |
| Docker not running / image missing | `FAILED_SAFE` with the fix; never reported as "the PR's tests failed" |
| GitHub rejects inline comments (422) | comments are folded into the review body instead |
| autofix push fails | logged, review still posted |
| PR head moves mid-run | stops; the next webhook reviews the new head |
| closed PR | skipped |
| model comments on lines not in the diff | dropped, logged as a `decision` |
| sandbox timeout | container/process killed, check reported as `timeout` |
| budget (tool calls, LLM calls, cost, wall time) | `BUDGET_STOP` + summary comment |
| ambiguous PR | `NEEDS_HUMAN`: safe checks and review still run, one question, label `fr:needs-human`, **no code changes** |
| same PR head SHA again | skipped (idempotency store) |
| PR from an `agent/*` branch | skipped (no review loops) |
| kill switch | `KILLED`: in-flight call cancelled, no further writes |
