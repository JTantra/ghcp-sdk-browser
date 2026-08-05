---
name: recovery-playbook
description: Propose schedule-recovery options for at-risk shipments.
---

# Recovery playbook

For each shipment handed to you, propose recovery options in priority order.

## Levers (cheapest first)
1. **Berth priority** at the next node — up to 8h recovered, low cost.
2. **Crane reallocation** — up to 14h, medium cost, needs node capacity.
3. **Port call swap** — up to 20h, high cost, requires carrier consent.
4. **Port call omission** — up to 30h, very high cost, commercial escalation.

## Rules
- Never propose a lever whose recovery exceeds the shipment's delay.
- Always call the mock ops-data tool for node capacity before recommending
  crane reallocation. If capacity is unknown, say so rather than assuming.
- Cap at two levers per shipment; more is not actionable in one shift.

## Output
Strict JSON:

```json
[{"ref":"SH-0000","options":[{"lever":"berth priority","recovers_h":6,
  "cost":"low","node":"...","note":"..."}]}]
```
