"""Run Foundry Evaluations against the deployed ghcp-sdk-browser agent.

Results upload to the Foundry portal (Evaluations blade) via `azure_ai_project`.

What makes this runnable unattended
-----------------------------------
Every browser tool relays to a human operator's extension, so `/invocations`
answers **409 "no operator browser attached"** before the model ever runs.
`headless_operator.HeadlessOperator` speaks the same relay protocol against a
frozen fixture board, so an eval run needs no Chrome and no human -- and is
deterministic, which scoring requires.

Usage:
    .venv/bin/python run_eval.py --name smoke-baseline
    .venv/bin/python run_eval.py --name quick --only board-read-count,risk-scoring-fidelity
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import uuid
from typing import Any

from dotenv import load_dotenv

from azure.ai.evaluation import (
    CoherenceEvaluator,
    FluencyEvaluator,
    IntentResolutionEvaluator,
    ResponseCompletenessEvaluator,
    TaskAdherenceEvaluator,
    ToolCallAccuracyEvaluator,
    evaluate,
)

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from headless_operator import HeadlessOperator  # noqa: E402
from delegation_evaluator import DelegationEvaluator  # noqa: E402

HERE = pathlib.Path(__file__).parent
TOOL_DEFS = json.loads((HERE / "tool_definitions.json").read_text())

#: Rows whose agent call failed outright (HTTP 5xx, timeout, relay failure).
#: Collected so the run can ABORT rather than publish scores computed from
#: empty responses -- see the check at the end of main().
_HARNESS_ERRORS: list[str] = []


def _credential():
    """Credential for both the agent call and the judge model.

    DefaultAzureCredential covers both worlds: locally it picks up `az login`,
    and in GitHub Actions it picks up the OIDC federated credential exported by
    azure/login as AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_FEDERATED_TOKEN_FILE.
    Shelling out to `az account get-access-token` (the original approach) works
    locally but is fragile on a runner.
    """
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


def _token() -> str:
    """Bearer token for the Foundry data plane.

    Audience is https://ai.azure.com -- NOT cognitiveservices.azure.com, which
    returns 'audience is incorrect'. Subscription policy forces
    disableLocalAuth=true on Cognitive Services, so there is no API-key path.
    """
    return _credential().get_token("https://ai.azure.com/.default").token


def _judge_config() -> dict[str, Any]:
    """Judge model config. Must not be the model under test.

    The agent runs claude-sonnet-5 via GitHub Copilot; the judge is a Foundry
    deployment (gpt-4.1 by default), so judge and subject never share weights.

    No key and no `credential` key in here: subscription policy sets
    disableLocalAuth=true, and AAD is passed via the evaluators' own
    `credential=` constructor argument (see _judge_credential).
    """
    return {
        "azure_endpoint": os.environ["JUDGE_ENDPOINT"],
        "azure_deployment": os.environ.get("JUDGE_MODEL_DEPLOYMENT", "gpt-4.1"),
        "api_version": os.environ.get("JUDGE_MODEL_API_VERSION", "2024-10-21"),
    }


def _judge_credential():
    """AAD credential for the judge model.

    This belongs on the EVALUATOR constructor (`credential=`), not inside
    model_config. Two dead ends worth not repeating:
      - `credential` inside model_config -> the library's own validator
        crashes: isinstance(v, typing.Any) TypeError (utils.py:421).
      - a raw bearer token as `api_key` -> AOAI 401, because the token is then
        sent as the `api-key` header instead of `Authorization: Bearer`.
    """
    return _credential()


#: agents.json display_name -> name. `subagent_started` frames carry DISPLAY
#: names ("Risk Analyst") while `task` tool calls carry CONFIG names
#: ("risk-analyst"), so both are normalised to the config name the golden
#: set asserts.
_SUBAGENT_ALIASES = {
    "risk analyst": "risk-analyst",
    "recovery planner": "recovery-planner",
    "reporter": "reporter",
}


def _norm(name: Any) -> str:
    key = str(name or "").strip().lower()
    return _SUBAGENT_ALIASES.get(key, key)


def _tool_name(frame: dict) -> str | None:
    """Name to report for one `tool_started` frame.

    `task` is the SDK's subagent-delegation primitive. It IS a genuine tool
    call the agent made, but the raw name is absent from tool_definitions.json,
    so reporting it verbatim made every delegating row look like a call to an
    undefined tool. Relabel to the declared per-subagent name.

    One frame -> one call: nothing is merged, nothing invented, and the agent
    itself is untouched (this harness only reads the stream).
    """
    name = frame.get("name")
    if name != "task":
        return name
    agent = _norm((frame.get("arguments") or {}).get("agent_type", ""))
    return f"delegate_to_{agent.replace('-', '_')}" if agent else None


def _make_target(agent_base: str):
    """Build the callable `evaluate()` invokes for each dataset row.

    A fresh relay session per row keeps rows independent -- the agent persists
    conversation state per session, so a shared one would let row N see row
    N-1's history and quietly inflate scores.
    """
    token = _token()

    def target(query: str) -> dict[str, Any]:
        # Signature is introspected by evaluate() to decide which dataset
        # columns to pass, so it must name exactly the columns wanted --
        # a **kwargs catch-all is read as a required column named '_'.
        sid = f"eval{uuid.uuid4().hex[:16]}"
        try:
            with HeadlessOperator(agent_base, token, sid) as op:
                out = op.ask(query)
        except Exception as exc:  # noqa: BLE001 - a dead row must not kill the run
            # Record the failure as DATA, not as a response. Returning the
            # error string as `response` let the judges score it ~1.0 and the
            # run looked like a catastrophic quality regression when the real
            # cause was an upstream HTTP 500 (GitHub Copilot token endpoint
            # 503). A dead backend must never masquerade as a bad agent.
            _HARNESS_ERRORS.append(f"{type(exc).__name__}: {exc}")
            return {"response": "", "tool_calls": [], "subagents": [],
                    "harness_error": f"{type(exc).__name__}: {exc}"}

        # ToolCallAccuracy wants OpenAI-shaped tool_calls.
        #
        # Source is the SSE `tool_started` frames, NOT the fixture browser's
        # own call log and NOT the server's activity[]: both only ever see
        # BROWSER tools, because hub.record() is called solely from
        # hub.dispatch_tool (hub.py:74). ops_node_capacity / ops_carrier_policy
        # run in-process on the subagents and never touch the relay, so
        # reporting only relayed calls made every ops_* row look like the agent
        # had skipped the tool -- scoring the harness, not the agent.
        # `tool.execution_start` fires for every tool (agents.py:641).
        tool_calls = [
            {"type": "tool_call", "name": n, "arguments": f.get("arguments") or {}}
            for f in out.get("frames", [])
            if f.get("type") == "tool_started"
            and (n := _tool_name(f)) is not None
        ]
        if not tool_calls:
            # Fallback: relayed browser calls, if the stream told us nothing.
            tool_calls = [
                {"type": "tool_call", "name": c["name"], "arguments": c["arguments"]}
                for c in out["tool_calls"]
            ]

        # Delegation order. `subagent_started` fires once per delegation in
        # dispatch order, so this is the real call sequence.
        subagents = [
            _norm(f["name"]) for f in out.get("frames", [])
            if f.get("type") == "subagent_started" and f.get("name")
        ]
        return {"response": out["response"], "tool_calls": tool_calls,
                "subagents": subagents}

    return target


def _prepare(src: pathlib.Path, only: set[str] | None) -> pathlib.Path:
    """Filter rows and attach tool schemas so ToolCallAccuracy can compare."""
    out = HERE / "results" / f"{src.stem}.prepared.jsonl"
    out.parent.mkdir(exist_ok=True)
    n = 0
    with src.open() as f_in, out.open("w") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if only and row.get("id") not in only:
                continue
            row.setdefault("tool_definitions", TOOL_DEFS)
            f_out.write(json.dumps(row) + "\n")
            n += 1
    if not n:
        raise SystemExit("no rows selected")
    print(f"prepared {n} row(s) -> {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="golden.jsonl")
    ap.add_argument("--name", required=True, help="Eval run display name")
    ap.add_argument("--only", default="", help="Comma-separated row ids")
    ap.add_argument("--gate", action="store_true",
                    help="Exit non-zero if thresholds.json floors are breached "
                         "(1 = threshold breach, 2 = harness/infra failure).")
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    project = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    agent_base = os.environ["AGENT_BASE_URL"].rstrip("/")

    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    prepared = _prepare(HERE / args.dataset, only)
    judge = _judge_config()
    cred = _judge_credential()

    result = evaluate(
        data=str(prepared),
        target=_make_target(agent_base),
        evaluation_name=args.name,
        evaluators={
            "intent":       IntentResolutionEvaluator(model_config=judge, credential=cred),
            "task":         TaskAdherenceEvaluator(model_config=judge, credential=cred),
            "tools":        ToolCallAccuracyEvaluator(model_config=judge, credential=cred),
            "completeness": ResponseCompletenessEvaluator(model_config=judge, credential=cred),
            "coherence":    CoherenceEvaluator(model_config=judge, credential=cred),
            "fluency":      FluencyEvaluator(model_config=judge, credential=cred),
            # Deterministic; no judge model. See delegation_evaluator.py.
            "delegation":   DelegationEvaluator(),
        },
        evaluator_config={
            "completeness": {"column_mapping": {
                "response": "${target.response}",
                "ground_truth": "${data.ground_truth}",
            }},
            "tools": {"column_mapping": {
                "query": "${data.query}",
                "tool_calls": "${target.tool_calls}",
                "tool_definitions": "${data.tool_definitions}",
            }},
            "delegation": {"column_mapping": {
                "expected_subagents": "${data.expected_subagents}",
                "subagents": "${target.subagents}",
            }},
        },
        # This is what publishes the run to the Foundry portal Evaluations blade.
        azure_ai_project=project,
        # ...and THIS is what makes it show on the AGENT's Evaluation tab.
        #
        # Without tags the run lands in the project-level evaluations list but
        # the agent page reads "No evaluations found": that view filters runs
        # by tags.targetId. Portal-created runs carry these three; SDK runs
        # default to tags={} and are invisible there. Verified by diffing an
        # SDK run against a portal-created one -- tags was the ONLY difference.
        # targetId must match the agent name exactly.
        tags={
            "targetType": "agent",
            "targetId": os.environ.get("AGENT_NAME", "ghcp-sdk-browser"),
            "trigger_type": "oneoff",
        },
        output_path=str(HERE / "results" / f"{args.name}.json"),
    )

    metrics = result.get("metrics") or {}

    print("\n=== summary ===")
    for metric, value in metrics.items():
        print(f"  {metric}: {value}")
    print("\nportal:", result.get("studio_url", "(no studio_url returned)"))

    _write_job_summary(args.name, metrics, result)

    # 1. Infrastructure failure ALWAYS wins over any score.
    #
    # A dead backend previously produced a full set of ~1.0 scores that looked
    # like a catastrophic quality regression (GitHub Copilot's token endpoint
    # returned 503, every row got HTTP 500). In CI that must fail as broken
    # infrastructure, never as a threshold breach -- hence a distinct exit code
    # and no threshold comparison at all.
    if _HARNESS_ERRORS:
        uniq = sorted(set(_HARNESS_ERRORS))
        print(f"\nHARNESS FAILURE: {len(_HARNESS_ERRORS)} row(s) never reached the agent.")
        for e in uniq[:5]:
            print(f"  - {e}")
        print("Scores above are meaningless; not gating on them.")
        raise SystemExit(2)

    if not args.gate:
        return

    # 2. Threshold gate. `$comment` keys are documentation (same convention as
    # agents.json) and must not be read as metrics.
    thresholds = {
        k: v for k, v in json.loads((HERE / "thresholds.json").read_text()).items()
        if not k.startswith("$comment")
    }
    failures = []
    print("\n=== gate ===")
    for metric, floor in sorted(thresholds.items()):
        actual = metrics.get(metric)
        if actual is None:
            failures.append(f"{metric}: MISSING from results")
            print(f"  MISSING {metric}")
            continue
        ok = actual >= floor
        print(f"  {'PASS' if ok else 'FAIL'}  {metric}: {actual:.2f} (floor {floor:.2f})")
        if not ok:
            failures.append(f"{metric}: {actual:.2f} < {floor:.2f}")

    if failures:
        print(f"\n{len(failures)} threshold(s) breached:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nall thresholds met")


def _write_job_summary(name: str, metrics: dict, result: dict) -> None:
    """Render a markdown table into the GitHub Actions job summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = result.get("rows", [])
    lines = [f"## Eval: `{name}`", ""]

    if _HARNESS_ERRORS:
        lines += [f"### ⚠️ Harness failure — {len(_HARNESS_ERRORS)} row(s) never reached the agent",
                  "", "Scores are not meaningful.", ""]
        for e in sorted(set(_HARNESS_ERRORS))[:5]:
            lines.append(f"- `{e}`")
        lines.append("")

    lines += ["| metric | value |", "|---|---|"]
    for k in sorted(metrics):
        if k.endswith("binary_aggregate") or "delegation" in k:
            lines.append(f"| `{k}` | {metrics[k]:.2f} |")

    lines += ["", "### Per-row delegation", "",
              "| row | recall | order | missing |", "|---|---|---|---|"]
    for r in rows:
        rid = r.get("inputs.id", "?")
        rec = r.get("outputs.delegation.delegation_recall")
        order = r.get("outputs.delegation.delegation_order_ok")
        miss = r.get("outputs.delegation.delegation_missing") or "—"
        rec_s = f"{rec:.2f}" if isinstance(rec, (int, float)) else "—"
        ord_s = f"{order:.0f}" if isinstance(order, (int, float)) else "—"
        flag = " ⚠️" if isinstance(rec, (int, float)) and rec < 1.0 else ""
        lines.append(f"| `{rid}` | {rec_s}{flag} | {ord_s} | {miss} |")

    url = result.get("studio_url")
    if url:
        lines += ["", f"[Open in Foundry]({url})"]

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
