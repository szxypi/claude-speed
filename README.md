# claude-speed

![CI](https://github.com/JuDaXia/claude-speed/actions/workflows/ci.yml/badge.svg)

**See Claude Code's real generation speed.** A macOS menu bar app + terminal statusline that separates the *true* tokens-per-second of your Claude Code responses from first-token latency — the number that actually fluctuates.

[中文文档 →](README.zh.md)

> Unofficial community tool. Not affiliated with Anthropic.

![Menu bar with dropdown — true speed, first-token latency, last response size](assets/menubar.png)

While a request is pending, the dropdown counts the wait live (`⏳等8秒`) — the title itself stays minimal.

```
Statusline: Fable 5 | ⚡71 tok/s 首字4.2s | 最近1022tok·22s | ctx 70%
```

## Why

Claude Code often *feels* "fast sometimes, slow sometimes". Most of that is a measurement illusion:

- Every API response pays a **fixed first-token cost (TTFT, typically 5–7s)**: queueing, prompt prefill, cache lookup.
- Naive speed = `output_tokens / total_time` therefore **collapses for short replies**: at a true 60 tok/s and 5s TTFT, a 300-token reply displays as 30 tok/s — half the real speed — while a 3000-token reply shows 55. Same server, same moment.

claude-speed fits `duration ≈ TTFT + tokens / TPS` across your recent responses (Theil-Sen regression, robust to outliers) and reports the two numbers separately:

- **TPS (slope)** — the model's true generation speed. Stable. This is the headline number.
- **TTFT (intercept)** — first-token latency. This is what actually fluctuates (cache misses, long context, server load), and what the tool alerts on.

## Components

| File | What it does |
|---|---|
| `collect.py` | Core collector: scans active session transcripts under `~/.claude/projects/`, prints menu bar title + dropdown lines |
| `main.swift` → `ClaudeSpeed` | Menu bar app (zero dependencies, compiled directly with `swiftc`), refreshes every 3s |
| `statusline-speed.py` | [Claude Code statusline](https://docs.anthropic.com/en/docs/claude-code/statusline) script — same math, rendered as one ANSI line under your prompt |

Works with every Claude Code client (CLI, desktop app, VS Code extension) — they all write the same transcripts. Reads local files only; no network access, nothing leaves your machine.

## Install

**Menu bar app (macOS):**

```bash
git clone https://github.com/JuDaXia/claude-speed && cd claude-speed
./install.sh                # menu bar app only
./install.sh --statusline   # + wire the Claude Code statusline
```

Requires Xcode Command Line Tools (`xcode-select --install`) and python3.

**What to expect right after install:** a **⚪ icon appears in the menu bar** (idle — no active session yet). Ask Claude Code anything and within a few seconds it becomes a live reading like `🟢71`; with `--statusline`, the speed line appears under the Claude Code input box on its next refresh — no restart needed. If nothing shows up, run `./collect.py`: it prints exactly what the menu bar would display.

Notes:
- The LaunchAgent points into the cloned directory — clone it anywhere, but if you later **move the directory, re-run `install.sh`**.
- `--statusline` backs up `~/.claude/settings.json` to `.bak` before editing.
- **Update:** `git pull && ./install.sh` (idempotent — recompiles and restarts).

**Statusline only (any platform, including Linux):** add to `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "/path/to/claude-speed/statusline-speed.py", "padding": 0 } }
```

**Uninstall:** `./uninstall.sh` (removes the LaunchAgent and statusline wiring).

## Reading the display

Menu bar title — `[⚠️][lamp+speed][ 🤖N]`, e.g. `⚠️🟢71 🤖3`:

| Symbol | Meaning |
|---|---|
| 🟢 / 🟡 / 🔴 | True generation speed ≥50 / ≥30 / <30 tok/s |
| `≈70` | Slope borrowed from other sessions of the same model (two-stage fit — new session, few samples yet) |
| `≥50` | Lower-bound estimate (not enough samples to split TTFT; true speed is at least this) |
| `⚪` | Idle (no response in 10 min) or speed uncertain |
| `⚠️` | API errors in the last 5 minutes |
| `🤖3` | 3 background subagents currently running (Task/research agents) |
| `🤖3 Σ140` | Background-only mode: no foreground reading, fleet burning 140 tok/s total |

The title stays minimal by design — waiting indicators live in the dropdown only: `⏳等N秒` counts up while a request is pending (disappears after 120s, assumed interrupted), and sessions with no response yet show `等待首个响应`.

Dropdown, one line per active session (up to 4, last 2 hours, **Claude Code and Codex CLI merged**):

```
myproject·fable5  🟢 71 tok/s 首字4s  缓存12%冷  ⚠️1错  最近1022tok·22s  4秒前
```

`首字Ns` = first-token latency · `·近N分` = fitting window when expanded beyond 10 min · `🤖N·Σtok/s` = N active background subagents and their combined burn rate · `缓存N%` = prompt cache hit rate of the last response (low values explain TTFT spikes; `冷`= cache write > read) · `⚠️N错` = API errors in last 30 min · `最近Ntok·Ns` = last response size/time · display text is currently Chinese — PRs welcome.

Statusline colors: speed green ≥50 / yellow ≥30 / red <30 · TTFT green ≤5s / yellow ≤12s / red >12s · ctx yellow ≥60% / red ≥85%.

## How it works

- Consecutive `assistant` records sharing one `message.id` in the transcript JSONL = one API response. Duration = last timestamp in the group − timestamp of the record preceding the group (≈ request send time), i.e. **including TTFT**. Token count = max `usage.output_tokens` in the group.
- Synthetic API-error records (`message.model == "<synthetic>"`) never form groups, but their timestamps still anchor the next group (a retry after an error is timed from the error, not the original request) and feed the ⚠️ indicators.
- **Per-model fitting**: only groups from the current model are fitted — mixing models mid-session skews the slope.
- **Outlier rejection**: groups with `duration/tokens > 0.5 s/tok` contain "user was typing" pauses and are dropped.
- **Sliding window**: fitting prefers the last 10 minutes of samples (server load drifts); the window doubles until the fit succeeds, and the effective span is labeled `·近N分`.
- **Two-stage fit**: with too few samples in one session, the slope (a model property) is borrowed from a cross-session pool; only the intercept (a session property) is fitted locally — new sessions show a speed after ~2 responses (`≈` marker).
- **Honest fallbacks**: when even that fails, long replies (≥300 tok) give a blended speed shown as a lower bound `≥N` (green only if the bound itself clears the green threshold); otherwise "insufficient samples". Large transcripts are tail-read (400 KB); malformed lines are skipped silently.
- **Background subagent monitoring**: research/Task agents write to `<session>/subagents/agent-*.jsonl` while the main transcript stays silent — a session's activity time is therefore `max(main transcript, newest subagent)`, so background work is never mistaken for idle. Agents with writes in the last 90s count as active; their combined output over the last 2 minutes gives the fleet burn rate (`🤖N Σtok/s`), and their response samples join the cross-session pool for two-stage fitting.
- **Codex CLI support** (menu bar only): Codex sessions live in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Each API response streams content records and is closed by a `token_count` event carrying full usage. Group start = timestamp of the record preceding the first content record (includes TTFT, same semantics as Claude); group end = the **last content record**, not the `token_count` event — that fires after tool execution and would inflate generation time. Model comes from `turn_context`, project label from `session_meta.cwd`, cache hit from `cached_input_tokens/input_tokens`. Everything downstream (fitting, windows, display) is reused as-is. Sessions with stale content are excluded even if the file was recently touched.

## Operations

```bash
# Rebuild after editing main.swift
swiftc -O -o ClaudeSpeed main.swift

# Restart the menu bar app — use kickstart, NOT unload/load:
# from a non-GUI context (SSH, agents, background shells) unload/load starts
# the app without a WindowServer connection: process alive, no icon, dead timer.
launchctl kickstart -k gui/$(id -u)/com.claude-speed.menubar

# Test the collector manually
./collect.py
```

Tunables are constants at the top of both Python scripts (thresholds, windows, session count).

## Development

```bash
python3 -m unittest discover -s tests -v
```

62 tests cover the fitting math (known-truth recovery, outlier rejection, window
expansion), end-to-end menu bar scenarios (waiting, errors, cold cache, two-stage
fit, background subagents and fleet burn proration), and a source-level AST check
enforcing that the shared algorithm stays byte-identical between `collect.py` and
`statusline-speed.py`. CI runs them on macOS and Linux plus a `swiftc` smoke build.

## License

[MIT](LICENSE)
