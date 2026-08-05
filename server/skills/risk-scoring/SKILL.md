---
name: risk-scoring
description: Score shipment delay risk from ops-board rows.
---

# Shipment risk scoring

Score each shipment 0-100. Higher = more operational risk.

## Inputs
Rows carrying: ref, vessel, route, eta, delay_hours, teu, status.

## Method
1. **Delay weight** — `min(delay_hours, 48) / 48 * 55` points.
2. **Status weight** — Delayed +25, At Risk +15, On Time +0.
3. **Exposure weight** — `teu / 10000 * 20` points, capped at 20.
4. Sum, clamp to 0-100, round to integer.

## Bands
- 70-100 `critical` — needs intervention this shift
- 45-69  `elevated` — monitor, prepare options
- 0-44   `nominal`  — no action

## Output
Strict JSON array, ordered by score descending:

```json
[{"ref":"SH-0000","vessel":"...","score":0,"band":"nominal","drivers":["..."]}]
```

`drivers` names the 1-3 factors that dominated the score. Never invent a row
that was not in the input.
