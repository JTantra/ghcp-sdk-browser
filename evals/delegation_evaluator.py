"""Deterministic delegation evaluator.

Foundry ships no delegation evaluator: `azure-ai-evaluation` 1.18.3 has
tool-call evaluators (ToolCallAccuracy, and the unexported _ToolSelection /
_ToolInputAccuracy / _ToolCallSuccess) but nothing that models an
agent-calls-agent edge. Its one agent-aware component, `AIAgentConverter`,
reads **Foundry Agent Service** thread run-steps; our subagents are in-process
GitHub Copilot SDK `custom_agents`, so it has nothing to read.

So delegation is scored here instead -- deterministically, on purpose.
Which subagents ran, and in what order, is a structural FACT recoverable from
the `subagent_started` SSE frames (agents.py emits one per delegation, in
dispatch order). An LLM judge would add cost and variance to a question that
has an exact answer.

This complements ToolCallAccuracy rather than duplicating it:
    delegation_recall  -> did the right subagents RUN?          (this file)
    tool_call_accuracy -> was delegating there APPROPRIATE?     (LLM judge)

Diagnostic value: when a row asserts `ops_node_capacity` and the tool is
missing, these two metrics separate the two possible causes --
    recall 0.0 -> the master never delegated        (fix the master prompt)
    recall 1.0 -> the subagent ran but skipped it   (fix the playbook skill)
"""
from __future__ import annotations

from typing import Any


class DelegationEvaluator:
    """Score subagent delegation against the golden set's expectations.

    Returns:
        delegation_recall      fraction of expected subagents that ran (0-1)
        delegation_order_ok    1.0 if those that ran did so in expected order
        delegation_missing     comma-separated expected-but-absent
        delegation_unexpected  comma-separated ran-but-not-expected
    """

    id = "custom://evaluators/delegation"

    def __call__(
        self,
        *,
        expected_subagents: Any = None,
        subagents: Any = None,
    ) -> dict[str, Any]:
        # No **kwargs catch-all: evaluate() introspects this signature to work
        # out which columns to supply, and reads a `**_` as a REQUIRED column
        # literally named '_' ("Some evaluators are missing required inputs:
        # - delegation: ['_']"). Name exactly the columns wanted, nothing more.
        expected = [str(s).strip().lower() for s in (expected_subagents or [])]
        actual = [str(s).strip().lower() for s in (subagents or [])]

        if not expected:
            # Rows asserting NO delegation (e.g. board-read-count): correct
            # only if the agent really delegated to nobody. Scoring these 1.0
            # unconditionally would hide a master that over-delegates.
            return {
                "delegation_recall": 1.0 if not actual else 0.0,
                "delegation_order_ok": 1.0,
                "delegation_missing": "",
                "delegation_unexpected": ",".join(sorted(set(actual))),
            }

        missing = [s for s in expected if s not in actual]
        recall = (len(expected) - len(missing)) / len(expected)

        # Order compares FIRST occurrence of each expected agent against the
        # expected sequence, ignoring repeats and extras: a planner invoked
        # twice, or an extra subagent in between, is not an ordering fault.
        seen: set[str] = set()
        first_order: list[str] = []
        for a in actual:
            if a in expected and a not in seen:
                seen.add(a)
                first_order.append(a)
        order_ok = float(first_order == [e for e in expected if e in seen])

        return {
            "delegation_recall": recall,
            "delegation_order_ok": order_ok,
            "delegation_missing": ",".join(missing),
            "delegation_unexpected": ",".join(
                sorted({a for a in actual if a not in expected})
            ),
        }
