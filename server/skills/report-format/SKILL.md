---
name: report-format
description: Format findings into the standard operations recovery brief.
---

# Recovery brief format

Produce markdown only. No preamble, no closing pleasantries.

```
## Recovery Brief — <UTC date>

**Reviewed:** N shipments · **Flagged:** M

### 1. <REF> — <Vessel>  (score NN, <band>)
<one sentence: what is wrong and why it matters>
- **Recommend:** <lever> at <node> — recovers ~Nh (<cost> cost)
- **Fallback:** <lever> — recovers ~Nh (<cost> cost)

### 2. ...

---
**Not flagged:** <refs, comma separated>
```

## Rules
- Order by score descending.
- One sentence per shipment for the situation. No padding.
- Every number must come from the data you were given. If a value is missing,
  write `unknown` — never estimate silently.
