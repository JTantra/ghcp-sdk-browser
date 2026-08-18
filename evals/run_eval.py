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

    _write_job_summary(args.name, metrics, result, _thresholds())

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
    thresholds = _thresholds()
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


def _thresholds() -> dict[str, float]:
    """Gate floors. `$comment` keys are documentation, not metrics."""
    return {
        k: v for k, v in json.loads((HERE / "thresholds.json").read_text()).items()
        if not k.startswith("$comment")
    }


def _row_evaluators(row: dict) -> dict[str, dict]:
    """Group one row's `outputs.<ns>.<metric>_*` fields by evaluator namespace.

    Derived from the keys rather than hardcoded: adding an evaluator to
    run_eval.py should widen the summary automatically, not silently omit it.
    """
    found: dict[str, dict] = {}
    for key, val in row.items():
        if not key.startswith("outputs.") or key.count(".") < 2:
            continue
        _, ns, field = key.split(".", 2)
        if field.endswith("_result"):
            found.setdefault(ns, {})["result"] = val
        elif field.endswith("_score"):
            found.setdefault(ns, {})["score"] = val
        elif field.endswith("_threshold"):
            found.setdefault(ns, {})["threshold"] = val
        elif field.endswith("_reason"):
            found.setdefault(ns, {})["reason"] = val
    return found


def _badge(ok: bool | None) -> str:
    if ok is None:
        return "⚪ n/a"
    return "🟢 PASS" if ok else "🔴 FAIL"


def _write_job_summary(name: str, metrics: dict, result: dict,
                       thresholds: dict | None = None) -> None:
    """Render the GitHub Actions job summary.

    The earlier version printed bare `binary_aggregate` values with no floor and
    no judge rationale, so a green run and a barely-passing one looked identical
    and a failure gave no clue WHY. Everything below already exists in the
    results JSON (`*_threshold`, `*_result`, `*_reason`); it was just unread.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    thresholds = thresholds or {}
    rows = result.get("rows", [])
    gated = sum(1 for m, f in thresholds.items()
                if isinstance(metrics.get(m), (int, float)) and metrics[m] >= f)
    overall = (not _HARNESS_ERRORS) and gated == len(thresholds)

    lines = [f"## Eval `{name}` — {'🟢 PASS' if overall else '🔴 FAIL'}", "",
             f"{len(rows)} row(s) · {gated}/{len(thresholds)} gated metrics met", ""]

    if _HARNESS_ERRORS:
        lines += [f"### ⚠️ Harness failure — {len(_HARNESS_ERRORS)} row(s) never reached the agent",
                  "", "Scores below are **not meaningful**.", ""]
        for e in sorted(set(_HARNESS_ERRORS))[:5]:
            lines.append(f"- `{e}`")
        lines.append("")

    # -- gated metrics: value vs floor, with headroom ----------------------
    lines += ["### Gate", "",
              "| | metric | score | floor | headroom |", "|---|---|---|---|---|"]
    for metric in sorted(thresholds):
        floor = thresholds[metric]
        actual = metrics.get(metric)
        if not isinstance(actual, (int, float)):
            lines.append(f"| ⚫ MISSING | `{metric}` | — | {floor:.2f} | — |")
            continue
        delta = actual - floor
        lines.append(f"| {_badge(actual >= floor)} | `{metric}` | **{actual:.2f}** | "
                     f"{floor:.2f} | {delta:+.2f} |")

    # -- ungated metrics: reported, explicitly not enforced ----------------
    extra = [m for m in sorted(metrics)
             if m.endswith("binary_aggregate") and m not in thresholds]
    if extra:
        lines += ["", "<details><summary>Informational (not gated)</summary>", "",
                  "| metric | pass rate | mean judge score |", "|---|---|---|"]
        for m in extra:
            ns = m.split(".")[0]
            mean = next((v for k, v in metrics.items()
                         if k.startswith(f"{ns}.") and k.endswith("_score")), None)
            mean_s = f"{mean:.2f} / 5" if isinstance(mean, (int, float)) else "—"
            lines.append(f"| `{m}` | {metrics[m]:.2f} | {mean_s} |")
        lines += ["", "</details>"]

    # -- per-row scorecard --------------------------------------------------
    namespaces = sorted({ns for r in rows for ns in _row_evaluators(r)})
    if rows:
        lines += ["", "### Rows", "",
                  "| row | " + " | ".join(f"`{ns}`" for ns in namespaces) +
                  " | delegation |",
                  "|---" * (len(namespaces) + 2) + "|"]
        for r in rows:
            evs = _row_evaluators(r)
            cells = []
            for ns in namespaces:
                ev = evs.get(ns, {})
                score, res = ev.get("score"), ev.get("result")
                # ToolCallAccuracy returns NaN on rows where it declines to
                # judge (no declared tool was called). NaN >= x is False
                # everywhere, so it must be caught explicitly or it renders
                # as a confident-looking "nan/3".
                if not isinstance(score, (int, float)) or score != score:
                    cells.append("⚪ n/a")
                    continue
                mark = "🟢" if res == "pass" else "🔴" if res == "fail" else "⚪"
                thr = ev.get("threshold")
                thr_s = f"/{thr:g}" if isinstance(thr, (int, float)) else ""
                cells.append(f"{mark} {score:g}{thr_s}")
            rec = r.get("outputs.delegation.delegation_recall")
            miss = r.get("outputs.delegation.delegation_missing")
            unexp = r.get("outputs.delegation.delegation_unexpected")
            exp = r.get("inputs.expected_subagents") or []
            if not isinstance(rec, (int, float)):
                deleg = "—"
            elif rec < 1.0 or miss:
                # Gated: a missing subagent is a real regression.
                deleg = f"🔴 recall {rec:.2f}" + (f"; missing `{miss}`" if miss else "")
            elif unexp:
                # NOT gated -- extra subagents are over-delegation, worth
                # surfacing but not a failure. Rendering these red made three
                # rows look broken inside a run that passed its gate.
                deleg = f"🟡 extra `{unexp}`"
            else:
                # No expected subagents is a real assertion (the master must NOT
                # delegate), so say which case passed rather than a bare tick.
                deleg = "🟢 " + (", ".join(exp) if exp else "none (correct)")
            lines.append(f"| `{r.get('inputs.id', '?')}` | " + " | ".join(cells) +
                         f" | {deleg} |")

    # -- judge rationale ----------------------------------------------------
    # Failures first and always expanded; passes collapsed. The reason text is
    # the only thing that tells you what to FIX, so it must never be a click
    # away when something is red.
    def _failed(r: dict) -> bool:
        return any(e.get("result") == "fail" for e in _row_evaluators(r).values())

    bad = [r for r in rows if _failed(r)]
    good = [r for r in rows if not _failed(r)]

    def _rationale(r: dict, failed_only: bool) -> list[str]:
        out = [f"**`{r.get('inputs.id', '?')}`** — {r.get('inputs.query', '')[:200]}", ""]
        for ns, ev in sorted(_row_evaluators(r).items()):
            reason = (ev.get("reason") or "").strip()
            # On a failing row, the passing judges' prose buries the one
            # verdict that explains the failure. Show only what went wrong.
            if not reason or (failed_only and ev.get("result") != "fail"):
                continue
            score, thr = ev.get("score"), ev.get("threshold")
            mark = "🟢" if ev.get("result") == "pass" else "🔴"
            head = f"{mark} **{ns}**"
            if isinstance(score, (int, float)) and score == score:
                head += f" {score:g}" + (f"/{thr:g}" if isinstance(thr, (int, float)) else "")
            # Judges prefix chain-of-thought with a fixed phrase; drop it.
            reason = reason.replace("Let's think step by step: ", "")
            out.append(f"- {head} — {reason[:600]}")
        if failed_only:
            resp = (r.get("outputs.response") or "").strip().replace("\n", " ")
            if resp:
                out += ["", f"> **agent said:** {resp[:400]}"]
        out.append("")
        return out

    if bad:
        lines += ["", "### 🔴 Judge rationale — failing rows", ""]
        for r in bad:
            lines += _rationale(r, failed_only=True)
    if good:
        lines += ["", f"<details><summary>Judge rationale — {len(good)} passing row(s)</summary>",
                  ""]
        for r in good:
            lines += _rationale(r, failed_only=False)
        lines += ["</details>"]

    url = result.get("studio_url")
    if url:
        lines += ["", f"[Open in Foundry]({url})"]

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
