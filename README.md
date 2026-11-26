# First Responder

[![ci](https://github.com/Vineet-Tripathi21/AgentWare/actions/workflows/ci.yml/badge.svg)](https://github.com/Vineet-Tripathi21/AgentWare/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![track](https://img.shields.io/badge/Agentware%202026-Track%205%20%C2%B7%20Developer%20Workflow-b6ff3b)

**An agent that does the first pass on every pull request, then stops.**

When a PR opens, First Responder reads the diff, runs the tests and linter in an isolated sandbox, flags risky changes, leaves line comments, pushes mechanical lint fixes to its own `agent/*` branch (only if tests still pass), and posts a summary. It never merges, approves, or pushes to your default branch. Those actions do not exist in its code.

![Live dashboard: a prompt-injected PR, with every forbidden request blocked](docs/dashboard.png)

| | |
|---|---|
| **Runs unattended** | GitHub webhook → full run → report, with no human step in the trace |
| **Knows when to stop** | contradictory PR → runs the safe checks, asks one question, changes no code · model overloaded → falls back to another free model, or to a clearly labelled rules-only review · budget/config/API/sandbox failure → reports and stops · kill switch → stops immediately |
| **Safety bonus** | ✅ guardrails in code · ✅ live dashboard · ✅ kill switch · ✅ hash-chained audit trail with replay |
| **Cost per test run** | **$0**: mock mode, or the free tiers of Gemini (`gemini-3.8-flash`) / Groq (`openai/gpt-oss-120b`), or local Ollama |

## How a run works

```mermaid
flowchart LR
    W["PR opened<br/>(GitHub webhook)"] --> T["1 · Read PR<br/>scan for injection<br/>deterministic risk rules"]
    T --> P["2 · Plan<br/>model returns JSON only"]
    P --> G{"Policy gate<br/>(enforced in code)"}
    G -->|allowed| A["3 · Check and review<br/>tests + lint in sandbox<br/>model review"]
    A --> C["4 · Verify fix<br/>autofix only if tests stay green"]
    C --> R["5 · Report<br/>labels · line comments · summary"]
    G -->|forbidden| X["Blocked and logged<br/>merge · approve · push main<br/>secrets · shell"]
    K["Kill switch"] -. cancels in-flight work .-> A
```

Every arrow above is a tool call that passes the policy gate, checks the kill switch, and is written to a hash-chained trace (`traces/*.jsonl`) before and after it runs.

---

## For judges: where to find each criterion

| Criterion | Evidence | See it in one command |
|---|---|---|
| **Autonomy (30%)** | `traces/*.jsonl`: webhook to final report with no human step; a contradictory PR gets the safe checks plus one question | `uv run fr replay <trace> --speed 1` |
| **Usefulness (25%)** | One narrow job done end to end: tests, lint, risk, line comments, a pushed fix branch | `uv run fr run --mock lint --show-actions` |
| **Reliability (20%)** | Trap pack 12/12 with the [mock model](tests/trap_pack/RESULTS.md) **and with [real Gemini](tests/trap_pack/RESULTS-gemini.md)** · reads PDF, Word, email, HTML and transcript traps · survived a real Gemini outage by falling back between models · failure table in [AGENT.md](AGENT.md) | `uv run fr trap-pack` |
| **Engineering (15%)** | [AGENT.md](AGENT.md) architecture · `src/fr/policy.py` · CI on Python 3.11 to 3.13 plus a real Docker sandbox job | `uv run pytest` |
| **Safety bonus (+4)** | [SAFETY.md](SAFETY.md): guardrails, live monitoring, kill switch, audit trail | `uv run fr serve`, then click **▶ slow** and press **■ KILL SWITCH** |

Setup for all of the above takes about 3 minutes and needs no keys (next section).

### Live evidence: real GitHub, real model, nobody at the keyboard

On 26 Nov 2026, `fr seed` opened four PRs on the public sandbox repo [`Vineet-Tripathi21/fr-sandbox`](https://github.com/Vineet-Tripathi21/fr-sandbox/pulls). The GitHub webhook triggered the agent for each one, with Gemini (free tier) and the Docker sandbox:

| PR | What the agent did | Trace |
|---|---|---|
| [#1 Add power()](https://github.com/Vineet-Tripathi21/fr-sandbox/pull/1) | Low risk, tests and lint passed, 1 line comment. When `gemini-3.8-flash` timed out mid-run, it switched to `gemini-3.6-flash` on its own | [`…180833-pr1`](traces/20261126-180833-pr1-e381.jsonl) |
| [#2 Add mean()](https://github.com/Vineet-Tripathi21/fr-sandbox/pull/2) | Lint errors → ran the fixer, re-ran the tests, then **pushed [`agent/fix-2-aa489cd`](https://github.com/Vineet-Tripathi21/fr-sandbox/tree/agent/fix-2-aa489cd)**. 3 line comments | [`…181121-pr2`](traces/20261126-181121-pr2-9a13.jsonl) |
| [#3 Speed up token check](https://github.com/Vineet-Tripathi21/fr-sandbox/pull/3) | Hardcoded admin password and a deleted test → **high risk**, no autofix | [`…181400-pr3`](traces/20261126-181400-pr3-abde.jsonl) |
| [#4 Docs: add usage examples](https://github.com/Vineet-Tripathi21/fr-sandbox/pull/4) | A prompt-injected PR. **The real model was partly fooled and rated it low risk with autofix on. The code-enforced layer overruled it:** risk raised to HIGH, autofix disabled, every injected instruction listed on the PR | [`…181425-pr4`](traces/20261126-181425-pr4-5ccf.jsonl) |

The first webhook-triggered attempt on each PR hit Gemini `503 "This model is currently experiencing high demand"` and **failed safe**: it stopped, wrote nothing but a "stopped early" note, and logged why ([`…175545-pr1`](traces/20261126-175545-pr1-5274.jsonl), [`…175626-pr2`](traces/20261126-175626-pr2-22f8.jsonl), [`…180110-pr3`](traces/20261126-180110-pr3-e49a.jsonl), [`…180149-pr4`](traces/20261126-180149-pr4-75e9.jsonl)). That outage is why the agent now falls back to other free models, and to a clearly labelled rules-only review if every model is down.

---

## Quick start: mock mode, no keys, about 3 minutes

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`). Docker is **not** needed for mock mode.

```bash
git clone https://github.com/Vineet-Tripathi21/AgentWare.git && cd AgentWare
uv sync                       # installs everything
uv run fr doctor              # checks your setup; WARN lines about live mode are fine here
uv run fr fixtures            # list the 7 demo PRs
uv run fr run --mock hostile  # watch it refuse a prompt-injected PR
uv run fr serve               # dashboard at http://127.0.0.1:8787
```

In the dashboard, click **▶ slow**, wait for the test suite to start, then press **■ KILL SWITCH**. Click any run under **Run history** to replay it from its trace, with the hash chain verified.

No uv? `python -m venv .venv`, activate it, `pip install -e .`, then run the same commands without `uv run`.

What mock mode is, honestly: GitHub is replaced by fixtures (`fixtures/`) and every write is recorded instead of sent. The model is replaced by a deterministic rules-based stand-in (`LLM_PROVIDER=mock`). The policy engine, sandbox, kill switch, audit trail and dashboard are **the real ones**. The mock model also deliberately behaves like a model that *was* fooled by prompt injection (it requests `merge_pr`, `read_secrets`…), so you can see the policy layer block those requests.

**Real model, still no GitHub:** put `LLM_PROVIDER=gemini` and a free `GEMINI_API_KEY` in `.env`, then run `uv run fr run --mock hostile`. The fixture PRs are reviewed by the real model; GitHub stays mocked.

### Demo PRs

| fixture | what it shows |
|---|---|
| `clean` | low risk, tests pass, short review |
| `lint` | lint errors → fix pushed to `agent/fix-102-*` after tests pass again |
| `risky` | auth change + hardcoded password + deleted test → **high risk**, blocker comments, no autofix |
| `hostile` | prompt injection in PR body, code comment and docs → forbidden requests **blocked**, labelled `fr:injection-detected` |
| `ambiguous` | contradictory description → runs the checks, asks one question, makes **no code changes** (`NEEDS_HUMAN`) |
| `broken` | failing test → diagnosis naming the failing test |
| `slow` | 45 s test suite → use it to demo the **kill switch** |

![Kill switch: the running test suite is cancelled mid-call and nothing is written](docs/kill-switch.png)

---

## Live mode: a real GitHub repo, about 15 minutes

Use a **throwaway sandbox repo** you own. Never point it at a repo you care about.

1. **Empty repo.** Create a new, empty GitHub repo (e.g. `you/fr-sandbox`). Don't add a README.
2. **Token.** Create a fine-grained personal access token: *Settings → Developer settings → Fine-grained tokens*. **Repository access: only `fr-sandbox`.** Permissions: *Contents: Read and write*, *Pull requests: Read and write*, *Issues: Read and write* (labels and summary comments), *Metadata: Read*. Nothing else.
3. **Model key (free).** Get a Gemini API key at <https://aistudio.google.com/apikey> (no card). Or a Groq key at <https://console.groq.com>, or run Ollama locally.
4. **Configure:**
   ```bash
   cp .env.example .env    # set LLM_PROVIDER=gemini, GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO, WEBHOOK_SECRET
   ```
5. **Sandbox image.** Start Docker Desktop, then run:
   ```bash
   uv run fr sandbox-build
   uv run fr doctor        # every line should be OK now
   ```
6. **Run unattended.** Start the dashboard and forward GitHub webhooks to it:
   ```bash
   uv run fr serve
   npx smee-client --url https://smee.io/<your-channel> --target http://127.0.0.1:8787/webhook
   ```
   In the sandbox repo, go to *Settings → Webhooks → Add webhook*. Payload URL: your smee URL. Content type: `application/json`. Secret: same as `WEBHOOK_SECRET`. Events: *Pull requests*.
7. **Open demo PRs and walk away:**
   ```bash
   uv run fr seed          # pushes the sandbox app to the empty repo and opens 4 demo PRs
   ```
   Each PR triggers the webhook and the agent reviews it on its own. Watch the dashboard. You can also run one by hand with `uv run fr run --pr 1` (add `--force` to re-run a PR it already reviewed).

### What a live test run costs
- Gemini / Groq free tier: **$0**. Each PR is 2 model calls (plan and review), about 3–8k tokens in total. Free tiers have per-minute limits; the agent retries on `429`.
- Paid tiers: set `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` and the dashboard shows the real cost. `MAX_COST_USD` (default $0.50) is a hard per-run cap.

---

## Commands

| command | does |
|---|---|
| `fr doctor` | checks Python, git, model key + model name, GitHub token + repo, Docker + sandbox image, kill switch |
| `fr serve` | webhook receiver + live dashboard (`--port 8787`) |
| `fr run --pr N` / `fr run --mock NAME` | one run, with a live timeline in the terminal (`--force` re-runs, `--show-actions` prints mock writes) |
| `fr seed [--fixtures a,b]` | live setup: push the sandbox app to an empty repo, open demo PRs (asks before doing anything) |
| `fr kill [reason]` / `fr resume` | engage or release the kill switch from any terminal |
| `fr replay <trace>` | re-render a run from its trace and verify its hash chain (`--speed 1` = real time) |
| `fr verify <trace>` | integrity check only |
| `fr trap-pack` | run every trap case and write `tests/trap_pack/RESULTS*.md` |
| `fr sandbox-build` | build the Docker sandbox image |

Dashboard links you can share: `/?run=<trace id>` opens a recorded run, `&filter=safety` jumps to the safety view.

## Tests

```bash
uv run pytest          # unit, end-to-end, live-path (real git + fake GitHub API), webhook, kill switch, Docker sandbox
uv run fr trap-pack    # hostile inputs; results in tests/trap_pack/RESULTS.md
```

`tests/test_live_path.py` runs the **live** code path without the internet: the real GitHub client, real `git` clone/commit/push, and real API payloads, pointed at a fake GitHub API and a local bare git repo. `tests/test_docker_sandbox.py` runs the real Docker sandbox; CI's `docker-sandbox` job fails if those tests would skip.

## Repo layout

```
src/fr/
  runner.py      the agent loop (TRIGGER → PLAN → ACT → CHECK → REPORT)
  policy.py      tool registry, tiers, validators, budgets, secret redaction. Every call is gated here
  killswitch.py  one switch, three triggers (button, CLI, file); cancels in-flight work
  audit.py       hash-chained JSONL trace + live event bus
  llm.py         gemini | groq | ollama | mock; schema-validated JSON only
  injection.py   untrusted-content wrapping + instruction detector
  risk.py        deterministic risk rules (can only raise risk)
  tools/github.py   real client (httpx + git) and fixture-backed fake
  tools/sandbox.py  docker (no network, capped, caps dropped, never pulls) or local (mock only)
  server.py      FastAPI webhook + SSE dashboard + run history
  dashboard/     the live monitoring page (single file, no build step)
  doctor.py      preflight checks · seed.py: live demo setup
fixtures/        sandbox repo + demo PRs for mock mode
tests/           unit + end-to-end + live-path + Docker tests, trap_pack/
traces/          real run logs
docs/            screenshots
```

## Honest limits

- **One repo, same-repo PRs only.** PRs from forks are refused, because running fork code with a write token is out of scope.
- **Python repos only** by default. The sandbox image has pytest and ruff. Other stacks need their own `.first-responder.toml` commands and image.
- **The injection detector is pattern-based.** It will miss novel phrasings. That is why it is not the main defence: the main defence is that the model cannot act, and every action goes through the policy gate regardless of what the model says.
- **Mock-mode reviews come from rules, not a model.** They are useful for exercising every code path, but the review text is only as good as the rules. Live mode, or mock GitHub with a real model, uses a real model.
- **Model names change, and free models get overloaded.** The defaults were checked against Google's and Groq's deprecation pages on 26 Nov 2026. On a live run on 26 Nov, `gemini-3.8-flash` returned `503 high demand`; the agent fell back to `gemini-3.6-flash` / `gemini-3.5-flash-lite` on its own and finished every review. Override with `LLM_MODEL` / `LLM_FALLBACK_MODELS`; `fr doctor` checks them.
- **The dashboard has no authentication.** Run it on localhost or behind your own auth.
- Line comments are only placed on lines **added** in the diff. Model comments pointing elsewhere are dropped and logged.

## Disclosure

Work on this project started before the official kickoff on 27 Nov 2026, with approval from the organisers. Commit history is left as-is. AI coding tools (Claude Code) were used, as the rules expect.

## License

MIT. See [LICENSE](LICENSE).
