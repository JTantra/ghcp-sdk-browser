"""Publish an already-scored eval run to the NEW Foundry portal.

Why this exists
---------------
There are two evaluation stores behind Foundry, and they do not talk to each
other:

    classic portal  <- AzureML run store   <- azure-ai-evaluation evaluate()
    new portal      <- /openai/v1/evals    <- OpenAI-compatible Evals API

`run_eval.py` writes to the first (its runs carry `_azureml.*` properties), so
nothing it produces is visible in the new portal. This script replays a
finished results JSON into the second one.

The awkward part
----------------
The Evals API is designed to RUN graders server-side; we already have every
score, threshold and judge rationale. So each of our metrics is expressed as a
`string_check` over a PRECOMPUTED `<ns>_result` field ("pass"/"fail"):

    {"type": "string_check", "input": "{{item.intent_result}}",
     "reference": "pass", "operation": "eq", "name": "intent"}

The grader is then a tautology -- it re-derives a verdict we already made. That
is the point: it is a no-op transport that makes the portal aggregate and
display OUR numbers without re-judging (which would cost tokens and, worse,
produce a second set of scores that could disagree with the gate).

Scores and judge reasons ride along as extra item fields so they show in the
row detail view.

All runs are added to ONE eval (`EVAL_NAME`) rather than each creating its own,
so the portal's Compare view -- side-by-side runs with t-testing -- has
something to compare. A fresh eval per run makes every run an island.

Usage:  python publish_to_evals_api.py results/ci-7.json
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx
from azure.identity import AzureCliCredential, DefaultAzureCredential
from dotenv import load_dotenv

# Namespaces to publish. delegation is deterministic (no judge); the rest are
# LLM-judged. Both end up as the same string_check shape.
JUDGED = ["intent", "task", "completeness", "coherence", "fluency", "tools"]


def _token() -> str:
    # Audience is ai.azure.com, same as run_eval.py -- NOT cognitiveservices.
    #
    # AzureCliCredential locally (fast, and matches how run_eval.py is driven
    # by hand); DefaultAzureCredential on a runner, where auth comes from the
    # OIDC federated login and there is no `az login` session to read.
    cred = DefaultAzureCredential() if os.environ.get("GITHUB_ACTIONS") \
        else AzureCliCredential()
    return cred.get_token("https://ai.azure.com/.default").token


def _row_items(rows: list[dict]) -> list[dict]:
    """One flat JSON object per eval row, matching item_schema below."""
    items = []
    for r in rows:
        item: dict[str, object] = {
            "id": str(r.get("inputs.id", "?")),
            "query": str(r.get("inputs.query", ""))[:2000],
            "response": str(r.get("outputs.response", ""))[:4000],
        }
        for ns in JUDGED:
            # Field names differ per evaluator (intent_resolution, fluency...),
            # so find them by suffix rather than hardcoding each one.
            res = score = reason = None
            for key, val in r.items():
                if not key.startswith(f"outputs.{ns}."):
                    continue
                if key.endswith("_result"):
                    res = val
                elif key.endswith("_score"):
                    score = val
                elif key.endswith("_reason"):
                    reason = val
            # NaN (ToolCallAccuracy declining to judge) is not a failure --
            # mark it skip so it does not drag the portal's pass rate down.
            if isinstance(score, float) and score != score:
                res, score = "skip", None
            item[f"{ns}_result"] = str(res) if res else "skip"
            item[f"{ns}_score"] = "" if score is None else f"{score:g}"
            item[f"{ns}_reason"] = str(reason or "")[:1500]

        rec = r.get("outputs.delegation.delegation_recall")
        item["delegation_result"] = (
            "pass" if isinstance(rec, (int, float)) and rec >= 1.0 else "fail"
        )
        item["delegation_detail"] = json.dumps({
            "expected": r.get("inputs.expected_subagents") or [],
            "recall": rec,
            "missing": r.get("outputs.delegation.delegation_missing") or "",
            "unexpected": r.get("outputs.delegation.delegation_unexpected") or "",
        })
        items.append(item)
    return items


#: Stable container for every CI run. Runs are added to THIS eval so the
#: portal's Compare view (t-testing across runs) works -- a fresh eval per run
#: makes each one an island and comparison impossible.
EVAL_NAME = "ghcp-sdk-browser"


def _find_or_create_eval(c: httpx.Client, ep: str, headers: dict,
                         schema: dict, criteria: list) -> str:
    """Resolve EVAL_NAME to an eval_id, creating it once if absent.

    Looked up BY NAME rather than stored in a repo variable: the id would be a
    second source of truth that silently breaks if the eval is ever deleted or
    recreated, and a CI job cannot repair it. Name lookup self-heals.
    """
    r = c.get(f"{ep}/openai/v1/evals", headers=headers)
    r.raise_for_status()
    for e in r.json().get("data", []):
        if e.get("name") == EVAL_NAME:
            print(f"reusing eval {e['id']} ({EVAL_NAME})")
            return e["id"]

    r = c.post(f"{ep}/openai/v1/evals", headers=headers, json={
        "name": EVAL_NAME,
        "data_source_config": {"type": "custom", "item_schema": schema,
                               "include_sample_schema": False},
        "testing_criteria": criteria,
        "metadata": {"source": "run_eval.py"},
    })
    if r.status_code >= 300:
        print("create eval failed:", r.status_code, r.text[:1200])
        raise SystemExit(1)
    eval_id = r.json()["id"]
    print(f"created eval {eval_id} ({EVAL_NAME})")
    return eval_id


def main() -> None:
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "results/ci-7.json")
    data = json.loads(src.read_text())
    rows = data.get("rows", [])
    name = src.stem

    # Same .env as run_eval.py -- one config source, not two. Without this the
    # endpoint is only defined for run_eval (which loads .env itself) and this
    # step dies with KeyError on the runner while the job still reports green.
    load_dotenv(pathlib.Path(__file__).parent / ".env")
    ep = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/")
    headers = {"Authorization": f"Bearer {_token()}",
               "Content-Type": "application/json"}
    items = _row_items(rows)

    props = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {k: props for k in items[0]},
        "required": ["id"],
    }
    criteria = [
        # `ne fail` rather than `eq pass`: rows the judge declined to score are
        # marked "skip", and `eq pass` would count those as FAILURES -- making
        # the portal harsher than the gate (ToolCallAccuracy returns NaN on
        # several rows, so this is the common case, not an edge case).
        # "did not fail" is the honest criterion for a precomputed verdict.
        {"type": "string_check", "name": ns,
         "input": f"{{{{item.{ns}_result}}}}", "reference": "fail",
         "operation": "ne"}
        for ns in JUDGED + ["delegation"]
    ]

    with httpx.Client(timeout=120) as c:
        eval_id = _find_or_create_eval(c, ep, headers, schema, criteria)

        r = c.post(f"{ep}/openai/v1/evals/{eval_id}/runs", headers=headers, json={
            "name": name,
            "data_source": {
                "type": "jsonl",
                "source": {"type": "file_content",
                           "content": [{"item": it} for it in items]},
            },
        })
        print("create run:", r.status_code)
        print(r.text[:1500])


if __name__ == "__main__":
    main()
