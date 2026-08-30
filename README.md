# codex-monitor

Aggregates Codex CLI/Desktop token usage from local rollout files and estimates
API-equivalent cost. No network access, zero dependencies (stdlib only), installable
as a [`uv` tool](https://docs.astral.sh/uv/concepts/tools/).

## How it works

Codex writes per-session rollouts to `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
(and `~/.codex/archived_sessions/`). Each `token_count` event carries a cumulative
`total_token_usage` for the session, so the **last** one per file is the session total.

Token accounting (verified against rollouts):

```
total_tokens              = input_tokens + output_tokens
reasoning_output_tokens   ⊂ output_tokens      (do NOT add)
cached_input_tokens       ⊂ input_tokens       (billed at cache-read rate)
```

Cost per model:

```
cost = uncached_in × price_in + cached_in × price_cached + out × price_out
```

Breakdowns: by model, by month, top projects (by `cwd`), by client (CLI vs Desktop).

## Install

```sh
cd codex-monitor
uv tool install --editable .    # editable: edits to the source apply immediately
# or a frozen copy: uv tool install .
```

Installs a `codex-monitor` command (needs `~/.local/bin` on `PATH`).

- One-off run without installing: `uvx --from /path/to/codex-monitor codex-monitor --days 30`
- After a non-editable install: `uv tool upgrade codex-monitor`
- Remove: `uv tool uninstall codex-monitor`

## Usage

```sh
codex-monitor                      # all sessions
codex-monitor --days 30            # last 30 days only
codex-monitor --prices my.json --top 20
codex-monitor --json               # machine-readable output
```

| Flag | Default | Description |
|---|---|---|
| `--roots` | `~/.codex/sessions;~/.codex/archived_sessions` | `;`-separated rollout dirs to scan |
| `--days N` | `0` (all) | only sessions from the last N days |
| `--prices FILE` | built-in table | JSON price overrides |
| `--top N` | `12` | number of projects to show |
| `--json` | off | emit JSON instead of tables |

## Prices

Built-in `PRICE_TABLE` is the public OpenAI API rate card (Aug 2026), USD per 1M
tokens (`in` / `cached` / `out`). Unknown models are priced at `DEFAULT_PRICE`
(gpt-5.6-sol rates) and flagged with `*` in the output.

Override or extend with a JSON file:

```json
{
  "gpt-5.6-sol":   {"in": 5.0, "cached": 0.5, "out": 30.0},
  "some-new-model": {"in": 1.0, "cached": 0.1, "out": 8.0}
}
```

```sh
codex-monitor --prices prices.json
```

## Caveats

- ChatGPT-plan usage is credit-based; the `$` figures are **API-equivalent, not billed**.
- `codex-auto-review` and `gpt-5.3-codex-spark` have no published rate card and
  reuse the gpt-5.3-codex rates — verify if this matters.
- Sessions with no `token_count` event are skipped.
