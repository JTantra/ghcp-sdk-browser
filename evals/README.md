# Evaluations — ghcp-sdk-browser hosted agent

Batch evaluation of the deployed `ghcp-sdk-browser` agent. Results upload to the
**Foundry portal → Evaluations** blade.

```bash
cd evals
cp judge_model.env.example .env       # values are already correct for this project
~/.local/bin/uv venv --python 3.12 .venv
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
az login

.venv/bin/python run_eval.py --name baseline-v1
.venv/bin/python run_eval.py --name quick --only board-read-count,report-format-fidelity
```

## The problem this had to solve first

Every browser tool relays to a human operator's Chrome extension, so
`/invocations` answers **409 `no operator browser attached`** before the model
runs at all. Measured directly:

```
POST .../invocations  {"input": {...}}
HTTP 409 {"error":"no operator browser attached for this session", ...}
```

That makes the agent un-evaluatable unattended. `headless_operator.py` solves it
by speaking the same relay protocol the extension speaks
(`extension/src/background/tools.ts`, `shared/protocol.ts`):

```
POST mode=attach       -> SSE stream; we read tool_call frames
POST mode=tool_result  -> resolve one call_id
POST (no mode)         -> synchronous turn -> {"output": "...", "activity": [...]}
```

The SSE reader runs on a daemon thread because the turn POST **blocks** on tool
calls that arrive over that stream — service them on the same thread and the run
deadlocks. (Same reason `start_turn()` returns 202 for the real extension.)

It serves a **frozen fixture board** copied verbatim from `demo-app/index.html`
rather than proxying the live demo app. Evaluation needs a fixed substrate;
otherwise scores move when demo data moves and a regression is
indistinguishable from a data edit.

## Ground truth is computed, not guessed

`risk-scoring/SKILL.md` is fully deterministic:

```
score = min(delay,48)/48*55  +  {Delayed:25, At Risk:15, On Time:0}  +  min(teu/10000*20, 20)
```

Applied to the real board, the only correct answer is:

| ref | delay | TEU | status | score | band |
|---|---|---|---|---|---|
| SH-4420 | 41 | 9400 | Delayed | **91** | critical |
| SH-4417 | 34 | 8200 | Delayed | **80** | critical |
| SH-4423 | 26 | 7100 | Delayed | **69** | elevated |
| SH-4425 | 18 | 4100 | At Risk | 44 | nominal |
| SH-4419 | 12 | 4700 | At Risk | 38 | nominal |
| SH-4422 | 7 | 5300 | At Risk | 34 | nominal |
| SH-4426 | 0 | 6600 | On Time | 13 | nominal |
| SH-4418 | 0 | 6100 | On Time | 12 | nominal |
| SH-4424 | 3 | 3900 | On Time | 11 | nominal |
| SH-4421 | 0 | 2200 | On Time | 4 | nominal |

**Reviewed 10, flagged 3.** Those numbers are asserted in `golden.jsonl`, so a
scoring regression is caught as a factual error rather than a style opinion.

> An earlier draft of `golden.jsonl` referenced `PSA-T2`, `PSA-T3`, `Maersk` and
> `KRPUS`. **None exist** — not on the board, not in `NODE_CAPACITY`, not in
> `CARRIER_POLICY` (which only has a `default` record). It was scoring the agent
> against invented facts. Rewritten against the real fixtures.

## The 10 golden rows

| id | what it pins |
|---|---|
| `board-read-count` | reads the board instead of answering from memory; 10 listed, 3 Delayed |
| `risk-scoring-fidelity` | exact scores/bands from the skill formula |
| `full-recovery-brief` | full master → risk-analyst → recovery-planner → reporter chain |
| `crane-realloc-capacity-guard` | `ops_node_capacity` **before** recommending cranes (Oakland: 9h, 1 berth) |
| `unknown-node-capacity` | Kaohsiung absent from the table → must say unknown, not assume |
| `lever-exceeds-delay` | playbook rule: never propose a 30h lever for a 7h delay |
| `carrier-policy-grounding` | grounds in `ops_carrier_policy` fields (notice_hours 12), invents nothing |
| `grounded-refusal-fabricated-premise` | rejects "assume 12 red shipments" |
| `master-delegation-boundary` | master delegates scoring even when told to bypass |
| `report-format-fidelity` | `report-format` skill structure exactly |

Kaohsiung, Jeddah and Alexandria are genuinely missing from `NODE_CAPACITY`,
which is what makes `unknown-node-capacity` a real test rather than a contrived one.

## Evaluators

Intent Resolution, Task Adherence, Tool Call Accuracy, Response Completeness,
Coherence, Fluency — judged by **gpt-4.1** deployed in this Foundry project.
The agent under test runs **claude-sonnet-5** via GitHub Copilot, so judge and
subject never share weights.

Plus **`DelegationEvaluator`** (`delegation_evaluator.py`) — deterministic, no
judge model, no cost.

`tool_definitions.json` carries the real schemas for all 8 tools; without it
Tool Call Accuracy has nothing to compare against.

Each row gets a **fresh relay session**. The agent persists conversation state
per session, so a shared one would let row N see row N-1's history and quietly
inflate scores.

## Capturing subagent delegation

Foundry ships **no delegation evaluator**. `azure-ai-evaluation` 1.18.3 has
tool-call evaluators (`ToolCallAccuracy`, plus unexported `_ToolSelection` /
`_ToolInputAccuracy` / `_ToolCallSuccess`) but nothing modelling an
agent-calls-agent edge. Its one agent-aware component, `AIAgentConverter`,
reads **Foundry Agent Service** thread run-steps — our subagents are in-process
GitHub Copilot SDK `custom_agents`, so it has nothing to read.

Delegation is therefore captured two ways, from the SSE stream. **Neither
changes the agent** — this harness only reads `/invocations`; `agents.json`,
`agents.py` and the deployed container are untouched.

**1. Delegation as declared tool calls.** The SDK already emits one tool call
per delegation, named `task`:

```
task {agent_type: 'risk-analyst',     description: 'Score SH-4420 risk', ...}
task {agent_type: 'recovery-planner', description: 'Propose recovery plan', ...}
```

Reported verbatim these score as calls to an undefined tool, because `task`
is not in `tool_definitions.json`. So each frame is relabelled to a declared
per-subagent name (`delegate_to_recovery_planner`, …) with a schema whose
description is lifted from `agents.json`. One frame → one call; nothing merged,
nothing invented. Tool Call Accuracy then judges whether delegating *there* was
appropriate.

> Caveat: `delegate_to_*` is a harness-side naming convention. It is faithful
> to what the agent did, but it will not grep to anything under `server/`.

**2. `DelegationEvaluator` — deterministic.** Which subagents ran and in what
order is a structural *fact*, recoverable from `subagent_started` frames (one
per delegation, in dispatch order). An LLM judge would only add cost and
variance to a question with an exact answer.

| metric | meaning |
|---|---|
| `delegation_recall` | fraction of `expected_subagents` that actually ran |
| `delegation_order_ok` | 1.0 if those that ran did so in the expected order |
| `delegation_missing` | expected but absent |
| `delegation_unexpected` | ran but not expected (catches over-delegation) |

Order compares the *first* occurrence of each expected agent, so repeats and
extra subagents are not ordering faults. Rows asserting no delegation score
1.0 only if the agent really delegated to nobody.

The two are complementary, and together they localise a failure:

```
row asserts ops_node_capacity, tool is missing
  delegation_recall 0.0  -> master never delegated      -> fix the master prompt
  delegation_recall 1.0  -> subagent ran, skipped tool  -> fix the playbook skill
```

Note `subagent_started` frames carry **display** names (`Risk Analyst`) while
`task` carries **config** names (`risk-analyst`); both are normalised via
`_SUBAGENT_ALIASES`.

**3. Not implemented here: trace-based online eval.** Subagent spans already
nest correctly in App Insights, so Foundry's continuous evaluation can sample
live traces for delegation structure without a golden set — the
production-monitoring complement to this batch suite.

## Gotchas that cost real time

- **Judge auth must be `credential=` on the evaluator constructor**, not in
  `model_config`. Subscription policy sets `disableLocalAuth=true`, so there is
  no key. Two dead ends:
  - `credential` inside `model_config` → the library's own validator crashes:
    `isinstance(v, typing.Any)` → `TypeError` (`_common/utils.py:421`).
  - bearer token as `api_key` → AOAI **401**, because it is then sent as the
    `api-key` header instead of `Authorization: Bearer`.
- **The target signature is introspected.** `def target(query, **_)` fails with
  `Missing required inputs for target: ['_']`. The same applies to **custom
  evaluators**: a `**_` catch-all on `__call__` yields
  `Some evaluators are missing required inputs: - delegation: ['_']`.
  Name exactly the columns you want, nothing more.
- **`/invocations` returns `output` as a STRING** (`main.py:260`), not
  `{"output": {"content": ...}}`. Parsing it as a dict silently scores every row
  against an empty response.
- **The evaluations REST API is `api-version=2025-05-15-preview`**, not `v1`
  (`v1` → `UnsupportedApiVersionValue`) and not `2025-07-31-preview`
  (→ `API version not supported`, despite being listed as supported).
- **`azure_ai_project` alone is NOT enough to show on the agent's page.** The
  run lands in the *project* evaluations list, but the **agent's Evaluation
  tab** reads `No evaluations found`, because that view filters by
  `tags.targetId`. Portal-created runs set these; SDK runs default to
  `tags={}`. Diffing an SDK run against a portal-created one showed `tags` was
  the **only** difference. Pass:
  ```python
  evaluate(..., tags={"targetType": "agent",
                      "targetId": "ghcp-sdk-browser",   # exact agent name
                      "trigger_type": "oneoff"})
  ```
- `studio_url` is built client-side and proves nothing. Verify a run landed
  *and is agent-visible*:
  ```
  GET {project}/evaluations/runs?api-version=2025-05-15-preview
  # then check tags.targetId == <agent name>
  ```

## Files

```
golden.jsonl           10 rows, ground truth computed from real fixtures
tool_definitions.json  schemas for 6 browser_* + 2 ops_* tools
headless_operator.py   synthetic operator; defeats the 409
run_eval.py            runner; uploads to the portal
results/               per-run scores (gitignored)
```
