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
import subprocess
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

HERE = pathlib.Path(__file__).parent
TOOL_DEFS = json.loads((HERE / "tool_definitions.json").read_text())


def _token() -> str:
    """AAD token for the Foundry data plane.

    Audience is https://ai.azure.com -- NOT cognitiveservices.azure.com, which
    returns 'audience is incorrect'. Subscription policy forces
    disableLocalAuth=true on Cognitive Services, so there is no API-key path.
    """
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


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
    from azure.identity import AzureCliCredential
    return AzureCliCredential()


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
            return {"response": f"[eval harness error: {type(exc).__name__}: {exc}]",
                    "tool_calls": []}

        # ToolCallAccuracy wants OpenAI-shaped tool_calls.
        tool_calls = [
            {"type": "tool_call", "name": c["name"], "arguments": c["arguments"]}
            for c in out["tool_calls"]
        ]
        # Browser tools are relayed and visible; ops_* tools run in-process on
        # the subagents, so recover them from the activity log the server keeps.
        for entry in out.get("activity", []):
            if entry.get("kind") == "tool_call" and entry.get("name", "").startswith("ops_"):
                tool_calls.append({"type": "tool_call", "name": entry["name"],
                                   "arguments": entry.get("arguments", {})})
        return {"response": out["response"], "tool_calls": tool_calls}

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
        },
        # This is what publishes the run to the Foundry portal Evaluations blade.
        azure_ai_project=project,
        output_path=str(HERE / "results" / f"{args.name}.json"),
    )

    print("\n=== summary ===")
    for metric, value in (result.get("metrics") or {}).items():
        print(f"  {metric}: {value}")
    print("\nportal:", result.get("studio_url", "(no studio_url returned)"))


if __name__ == "__main__":
    main()
