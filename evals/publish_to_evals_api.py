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

Usage:  python publish_to_evals_api.py results/ci-7.json
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx
from azure.identity import AzureCliCredential

HERE = pathlib.Path(__file__).parent

# Namespaces to publish. delegation is deterministic (no judge); the rest are
# LLM-judged. Both end up as the same string_check shape.
JUDGED = ["intent", "task", "completeness", "coherence", "fluency", "tools"]


def _token() -> str:
    # Audience is ai.azure.com, same as run_eval.py -- NOT cognitiveservices.
    return AzureCliCredential().get_token("https://ai.azure.com/.default").token


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


def main() -> None:
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "results/ci-7.json")
    data = json.loads(src.read_text())
    rows = data.get("rows", [])
    name = src.stem

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
        r = c.post(f"{ep}/openai/v1/evals", headers=headers, json={
            "name": f"ghcp-sdk-browser :: {name}",
            "data_source_config": {"type": "custom", "item_schema": schema,
                                   "include_sample_schema": False},
            "testing_criteria": criteria,
            "metadata": {"source": "run_eval.py", "run": name},
        })
        print("create eval:", r.status_code)
        if r.status_code >= 300:
            print(r.text[:1200])
            raise SystemExit(1)
        eval_id = r.json()["id"]
        print("eval_id:", eval_id)

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
