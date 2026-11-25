# First Responder

**An agent that does the first pass on every pull request, then stops.**

When a PR opens, First Responder reads the diff, runs the tests and linter in an isolated sandbox, flags risky changes, leaves line comments, pushes mechanical lint fixes to its own `agent/*` branch (only if tests still pass), and posts a summary. It never merges, approves, or pushes to your default branch. Those actions do not exist in its code.

Built for **Agentware 2026, Track 5: Developer Workflow**.

```
PR opened ──▶ TRIGGER ──▶ PLAN ──▶ ACT ──▶ CHECK ──▶ REPORT
  (webhook)   read PR    model     sandbox   autofix    labels, line comments,
              scan for   proposes, tests +   only if    summary with cost and
              injection  policy    lint,     tests stay what it refused to do
              + risk     decides   review    green
```

| | |
|---|---|
| **Runs unattended** | GitHub webhook → full run → report, with no human step in the trace |
| **Knows when to stop** | contradictory PR → runs the safe checks, asks one question, changes no code · budget/LLM/API/sandbox failure → reports and stops · kill switch → stops immediately |
| **Safety bonus** | ✅ guardrails in code · ✅ live dashboard · ✅ kill switch · ✅ hash-chained audit trail with replay |
| **Cost per test run** | **$0**: mock mode, or the free tiers of Gemini (`gemini-3.8-flash`) / Groq (`openai/gpt-oss-120b`), or local Ollama |

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

In the dashboard, click **▶ slow**, wait for the test suite to start, then press **■ KILL SWITCH**.

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

## Tests

```bash
uv run pytest          # unit, end-to-end, live-path (real git + fake GitHub API), webhook, kill switch
uv run fr trap-pack    # hostile inputs; results in tests/trap_pack/RESULTS.md
```

`tests/test_live_path.py` runs the **live** code path without the internet: the real GitHub client, real `git` clone/commit/push, and real API payloads, pointed at a fake GitHub API and a local bare git repo.

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
  server.py      FastAPI webhook + SSE dashboard
  doctor.py      preflight checks · seed.py: live demo setup
fixtures/        sandbox repo + demo PRs for mock mode
tests/           unit + end-to-end + live-path tests, trap_pack/
traces/          real run logs
```

## Honest limits

- **One repo, same-repo PRs only.** PRs from forks are refused, because running fork code with a write token is out of scope.
- **Python repos only** by default. The sandbox image has pytest and ruff. Other stacks need their own `.first-responder.toml` commands and image.
- **The injection detector is pattern-based.** It will miss novel phrasings. That is why it is not the main defence: the main defence is that the model cannot act, and every action goes through the policy gate regardless of what the model says.
- **Mock-mode reviews come from rules, not a model.** They are useful for exercising every code path, but the review text is only as good as the rules. Live mode, or mock GitHub with a real model, uses a real model.
- **Model names change.** The defaults were checked against Google's and Groq's deprecation pages on 26 Nov 2026. If a provider retires one, `fr doctor` says so; set `LLM_MODEL`.
- **The dashboard has no authentication.** Run it on localhost or behind your own auth.
- Line comments are only placed on lines **added** in the diff. Model comments pointing elsewhere are dropped and logged.

## Disclosure

Work on this project started before the official kickoff on 27 Nov 2026, with approval from the organisers. Commit history is left as-is.

## License

MIT. See [LICENSE](LICENSE).
