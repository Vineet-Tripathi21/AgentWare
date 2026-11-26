# SAFETY.md

Everything below is **enforced in code**. The prompts also tell the model to be careful, but nothing depends on that.

## 1. Guardrails: `src/fr/policy.py`

Every tool call passes `Policy.gate(tool, args)` before it runs (`runner.Agent._tool`).

- **Irreversible actions are not implemented.** Merge, approve, close, force-push, push to the default branch, delete branch, edit workflows, read secrets, run arbitrary shell, deploy: these are registered as `FORBIDDEN` so a request for them is logged and refused. Neither GitHub client (`tools/github.py`) has a method that could perform them.
- **Writes need a plan.** Until a validated plan exists, no WRITE tool is allowed. The runner, not the model, decides which writes the plan unlocks (`autofix` is never unlocked for high-risk PRs or PRs with injection hits).
- **Argument validators:** pushes only to `agent/*` (no `..`, no `.github/` paths); sandbox commands only by key (`test`/`lint`/`fix`); labels only `risk:*`/`fr:*`; comments refused if they contain secret-shaped strings.
- **Verify before push:** an autofix is pushed only if the full test suite passes on the fixed tree.
- **Least privilege:** fine-grained token on one repo; fork PRs refused; the sandbox gets a scrubbed environment (no tokens, no API keys) and in Docker has **no network** and **never pulls an image** (`--pull never`), so a same-named public image can't be substituted.
- **Secret redaction:** anything secret-shaped (GitHub/Google/AWS/OpenAI/Groq/Slack tokens, private keys) is replaced with `[REDACTED]` before it is posted to GitHub **and** before it is written to a trace, because traces are committed.
- **Exact commits:** an autofix commits exactly the file list the policy approved (`git add -- <files>`), from a workspace reset to the PR head first. The bot's git runs with hooks disabled and signing off, so a developer's personal git config can't run code in the agent.
- **Budgets:** max tool calls, LLM calls, USD cost and wall time per run. Exceeding any of them stops the run (`BUDGET_STOP`).
- **Prompt injection:** all PR text, diff, file content and check output is wrapped in nonce-tagged `<untrusted-…>` blocks (content cannot forge the closing tag) and scanned (`injection.py`). Hits are logged, labelled and reported, and they disable autofix. More importantly, even a fully fooled model can only emit JSON that the policy then checks. The `hostile` fixture and trap pack prove this: the mock model *asks* for `merge_pr`, `read_secrets` and `run_shell`, and all are blocked.

## 2. Live monitoring: `fr serve` → http://127.0.0.1:8787

A real-time view (Server-Sent Events) of what the agent is doing and why:
- **Header:** run status, the 5-phase stepper (Read PR → Plan → Check & review → Verify fix → Report), and live **tool calls / model calls / tokens / cost so far / elapsed**.
- **Timeline:** one plain-English row per step. Each tool call shows a spinner while running, then ✓/✗ with its duration and READ/WRITE tier. Decisions show their reason, and raw details expand. Filters: Actions · Decisions · Safety · Errors.
- **Outcome panel:** writes performed, actions blocked by policy (and who asked for them), injection hits, risk level with the reasons, checks run in the sandbox, and the hard limits.
- **Run history:** any past run replays from its trace with the hash chain re-verified (`✓ log intact`). Shareable links: `/?run=<id>`, `&filter=safety`.

`fr run` shows the same timeline in the terminal.

## 3. Kill switch: `src/fr/killswitch.py`

One switch, three triggers, one effect:
- **Dashboard:** the red **■ KILL SWITCH** button (`POST /api/kill`)
- **CLI:** `fr kill`, from any terminal
- **File:** create `.fr/KILL` (polled every 250 ms)

Effect: every running agent task is **cancelled immediately**. The in-flight tool call is interrupted, not awaited: a running test process or Docker container is killed, an HTTP request is abandoned. Queued jobs are dropped, and new runs are refused until `fr resume`. The trace records `tool_cancelled` and `run_killed`, and the run makes **no further writes** (not even a summary comment). Tested in `tests/test_agent.py::test_kill_switch_cancels_mid_run`: a 45 s test suite is killed and the run stops in under 3 s with zero GitHub writes.

## 4. Audit trail: `src/fr/audit.py`

- One append-only JSONL file per run in `traces/`. Every event has `seq`, `ts`, `phase`, `type`, `data`, `prev` (hash of the previous event) and `hash` (sha256 of its own canonical content plus `prev`).
- Logged for every step: tool name, tier, arguments, result summary, latency, model outputs (full JSON), token and cost counters, policy blocks with reasons, decisions with reasons, injection hits, final status.
- `fr verify <trace>` detects any edited, removed or reordered line. Tested in `test_units.py`.
- `fr replay <trace> [--speed 1]` re-renders the run step by step.

## Known gaps (not fixed)

- The injection detector is regex-based and can be evaded. It is a tripwire, not the boundary; the policy gate is the boundary.
- Local sandbox mode (`SANDBOX=local`) runs PR code on the host. It is refused outside mock mode, where the code is our own fixtures.
- The dashboard has no auth; bind it to localhost.
- A *malicious maintainer* could change `.first-responder.toml` in a PR to alter the test command. In Docker it still has no network and no secrets, and the agent will not push changes to that file. Hardening idea: read the config from the base branch instead of the PR head.
