# claude-speed measurement standard — METRIC v1.2

This document is the **definition** of the number claude-speed displays — the
written spec, like the text that defines the metre. The displayed speed is not
measured directly; it is *estimated* from timestamps and token counts under the
conventions fixed here. Changing any of them changes the number, so changing
this document is a deliberate, versioned act (see **Recalibration**).

The estimator is **not absolutely accurate.** Token counts are exact (from the
API's `usage`), but timestamps are "record-written" times (tens to hundreds of
ms of slack vs. the wire), and TPS/TTFT are the *output of a fit*, not a direct
reading. What this standard guarantees is **not** absolute accuracy — it is
**stability**: the same input always yields the same number, and that number
only moves when a human deliberately recalibrates and records the shift. For a
monitoring tool, comparability over time matters more than absolute truth.

## Definition

One **API response** = consecutive records that belong to a single model
generation (Claude: same `message.id`; Codex: content records between turn
boundaries; Kimi: one `llm.request` → `step.end` pair; OpenCode: one completed
assistant message). For each response:

- **out** = `usage.output_tokens` (the max within the group; exact. Kimi:
  `step.end`'s `usage.output`; OpenCode: `tokens.output + tokens.reasoning`).
- **duration** = `end − start`, where
  - **start** = timestamp of the record immediately preceding the group's first
    content record (≈ request send time) — so duration **includes TTFT**
    (Kimi: the `llm.request` record's own `time`, which *is* the request send
    time; wire `time` fields are epoch-milliseconds strings);
  - **end** = timestamp of the group's **last content record** — never a
    trailing bookkeeping record (Codex `token_count` fires after tool execution
    and would fold tool time into generation. Kimi: the closing `step.end`
    record, which marks response consumption complete).
  - Kimi retries: a new `llm.request` discards any unclosed earlier request —
    the anchor moves to the retry, mirroring Claude's error-anchor rule.
  - OpenCode: start is the assistant message's `time.created`; end is the latest
    text/reasoning `time.end` or tool-state `time.start` (falling back to
    `time.completed` when no content timing exists). Subtract the **union** of
    tool-state intervals before that boundary. Unioning first prevents
    overlapping/parallel tools from being deducted twice and keeps local
    `bash`, `task` and `question` execution time out of model-generation time.

OpenCode v1.18.15 Desktop and CLI share the XDG data database at
`$XDG_DATA_HOME/opencode/opencode.db` (`~/.local/share/opencode/opencode.db` by
default on macOS). The database and its WAL are read-only inputs. Usage metadata
maps from `tokens.input`, `tokens.cache.read` and `tokens.cache.write`; the model
key is `providerID/modelID`, and the project label is `session.directory`.
Sessions with a `parent_id` are background agents. OpenCode is a menu-bar source
only, with no statusline wiring. Its database format and schema are internal
implementation details; an unrecognized schema makes the source silently
unavailable rather than failing the collector.

Across a set of responses, model:

    duration ≈ TTFT + out / TPS

- **TPS** (tokens/second) = 1 / slope. The model's true generation speed. The
  headline number. Stable across reply lengths by construction.
- **TTFT** (seconds) = intercept. First-token latency. The quantity that
  actually fluctuates.

The slope is estimated by **Theil-Sen regression** (median of pairwise slopes;
robust to outliers). The intercept is the median residual, floored at 0.

## Parameters (part of the standard — changing any bumps the version)

| Constant | Value | Role |
|---|---|---|
| `MAX_SEC_PER_TOK` | 0.5 | Sample lower bound: `dur/out ≥ 0.5s` (<2 tok/s) is a "user was typing" pause, dropped. |
| `MAX_TPS` | 400 | Sample upper bound + fit acceptance ceiling: `out/dur > 400` is a collapsed-timestamp "instant group", dropped. |
| `FIT_MIN_SAMPLES` | 5 | Fewer valid points → no split (fall back to lower bound). |
| `FIT_MIN_SPAN` | 150 | Output-token span must reach this for the regression to carry information. |
| `FIT_MIN_PAIR_DX` | 50 | Theil-Sen uses only pairs whose x differ by ≥ this (small denominators amplify noise). |
| `FIT_WINDOW_START` | 600 | Sliding window prefers the last 10 min; doubles until the fit succeeds. |

Fit acceptance: a slope is used only if `3 ≤ TPS ≤ MAX_TPS`. Below the sample
floor: with a ≥300-token reply, the blended rate `out/dur` is reported as a
**lower bound** (`≥N`, since it is strictly below true TPS); otherwise
"insufficient samples".

Reasoning/thinking tokens are part of "generation" here under each source's
convention. OpenCode stores them separately, so its `out` explicitly adds
`tokens.reasoning`.

## The physical standard

`tests/fixtures/*.jsonl` are **frozen source-record bytes** — transcript records
for Claude/Codex/Kimi and the deterministic message/part metadata projection
read from SQLite for OpenCode. They are committed and never regenerated at test
time. `tests/golden.json` is their **certified value**: for each fixture, the
estimator's reading plus, for synthetic ones, the known ground-truth generation
parameters.

Fixtures are synthetic by design: no real transcript content (privacy), and a
*known* true TPS/TTFT so the registry doubles as a calibration table. Pathology
fixtures ("steady samples + one poison record") encode real failure modes found
in review — an instant group, an unclosed cross-turn merge — so a regression
that lets the poison through shifts the reading and trips the test.

`tests/test_golden.py` enforces two things on every CI run:

1. **Drift** — reading vs. `golden.json` within `drift_tol` (tps ±0.5, ttft
   ±0.2). Any code change that moves the number turns the suite red.
2. **Calibration** — reading vs. ground truth within `calib_tol` (tps ±3%, ttft
   ±0.5s). Guards against a *biased* recalibration (re-pinning the registry to a
   wrong value).

At METRIC v1.2 the estimator recovers every synthetic fixture's truth exactly
(70/5, 45/8, 90/4, 80/6, 85/5). All pre-v1.2 fixtures retain their certified readings
(zero drift).

## Recalibration

When you deliberately change the algorithm and the reading moves:

```bash
python3 tests/gen_fixtures.py    # only if you are adding/changing fixtures
python3 tests/regen_golden.py    # recompute the certified readings
git diff tests/golden.json       # SEE the shift, fixture by fixture
```

Then bump `METRIC_VERSION` in `regen_golden.py` (and the heading of this file)
and commit with the shift documented — e.g. "recalibrate: outlier filter tightened,
codex readings −38% on instant-group fixture; METRIC v1.0 → v1.1". The
ground-truth values in the SPEC do not change, so the diff isolates exactly how
much the basis moved. This is the tool's equivalent of a CODATA bulletin: the
standard may be revised, but never silently.

Version history:

- **v1.0** — initial standard (Claude + Codex sources).
- **v1.1** — added the Kimi Code source definition (`llm.request` → `step.end`
  pairing, epoch-ms `time` fields, retry anchoring). Existing Claude/Codex
  fixture readings unchanged (zero drift, verified by `regen_golden.py`).
- **v1.2** — added the OpenCode Desktop/CLI source definition (completed
  assistant messages, reasoning-token inclusion, and tool-interval union
  subtraction). Existing fixtures are unchanged (zero drift).

## What this standard does not cover

Absolute TTFT truth. Timestamps approximate the wire; the fitted intercept is an
estimate, not a certified latency. To pin the estimator against an external
reference, enable Claude Code's OpenTelemetry export
(`CLAUDE_CODE_ENABLE_TELEMETRY=1`); its `api_request` events carry authoritative
`ttft_ms` / `duration_ms`. Comparing the fit to those over a day of real traffic
would quantify the systematic bias — a one-off calibration experiment, not part
of this repo.
