"""codex-monitor: aggregate Codex CLI/Desktop token usage from rollout files.

Codex writes per-session rollouts to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
(and ~/.codex/archived_sessions/). Each `token_count` event carries a cumulative
`total_token_usage` for the session, so the LAST one per file is the session total.

Token accounting (verified against rollouts):
    total_tokens = input_tokens + output_tokens
    reasoning_output_tokens  subset of output_tokens   (do NOT add)
    cached_input_tokens      subset of input_tokens    (billed at cache-read rate)

Cost = uncached_in * price_in + cached_in * price_cached + output * price_out

Usage:
    codex-monitor                      # all sessions
    codex-monitor --days 30            # last 30 days only
    codex-monitor --prices my.json --top 20
    codex-monitor --json               # machine-readable output

Prices are USD per 1M tokens (public API rate card, Sep 2026). Override with a
JSON file: {"model": {"in": 5.0, "cached": 0.5, "out": 30.0}}.
Unknown models price at DEFAULT_PRICE and are flagged with *.
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

# USD per 1M tokens: input / cached-read / output
PRICE_TABLE = {
    "gpt-6-astra":        {"in": 10.00, "cached": 1.00,  "out": 50.00},
    "gpt-5.6-sol":        {"in": 5.00,  "cached": 0.50,  "out": 30.00},
    "gpt-5.6-terra":      {"in": 2.00,  "cached": 0.20,  "out": 12.00},
    "gpt-5.6-luna":       {"in": 0.20,  "cached": 0.02,  "out": 1.20},
    "gpt-5.5":            {"in": 5.00,  "cached": 0.50,  "out": 30.00},
    "gpt-5.4":            {"in": 2.50,  "cached": 0.25,  "out": 15.00},
    "gpt-5.4-mini":       {"in": 0.75,  "cached": 0.075, "out": 4.50},
    "gpt-5.3-codex":      {"in": 1.75,  "cached": 0.175, "out": 14.00},
    "gpt-5.2-codex":      {"in": 1.75,  "cached": 0.175, "out": 14.00},
    # codex-auto-review runs the code-review model (5.3-codex rate card);
    # gpt-5.3-codex-spark has no published rate -> same card. Verify if unsure.
    "codex-auto-review":  {"in": 1.75,  "cached": 0.175, "out": 14.00},
    "gpt-5.3-codex-spark": {"in": 1.75, "cached": 0.175, "out": 14.00},
}
DEFAULT_PRICE = {"in": 5.00, "cached": 0.50, "out": 30.00}

USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")


def load_sessions(roots):
    files = []
    for root in roots:
        files += glob.glob(os.path.expanduser(root + "/**/*.jsonl"), recursive=True)
    sessions, seen = [], set()
    for fp in files:
        real = os.path.realpath(fp)
        if real in seen:
            continue
        seen.add(real)
        ts = cwd = origin = None
        last_tc = None
        model_votes = defaultdict(int)
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"session_meta"' in line:
                        try:
                            p = json.loads(line).get("payload") or {}
                            ts, cwd, origin = p.get("timestamp"), p.get("cwd"), p.get("originator")
                        except (ValueError, KeyError):
                            pass
                    elif '"token_count"' in line:
                        try:
                            p = json.loads(line)["payload"]
                            if p.get("type") == "token_count" and p.get("info"):
                                last_tc = p["info"]
                        except (ValueError, KeyError):
                            pass
                    elif '"turn_context"' in line:
                        try:
                            m = json.loads(line)["payload"].get("model")
                            if m:
                                model_votes[m] += 1
                        except (ValueError, KeyError):
                            pass
        except OSError:
            continue
        if last_tc is None:
            continue
        tu = last_tc.get("total_token_usage") or {}
        model = max(model_votes, key=model_votes.get) if model_votes else "?"
        s = {"file": fp, "ts": ts or "?", "cwd": cwd or "?",
             "origin": origin or "?", "model": model}
        s.update({k: int(tu.get(k, 0) or 0) for k in USAGE_KEYS})
        sessions.append(s)
    return sessions


def price_of(model, table):
    return table.get(model), model in table


def cost_of(tok, model, table):
    """tok: dict with input/cached/output sums for one model's usage; returns USD."""
    p, _known = price_of(model, table)
    p = p or DEFAULT_PRICE
    uncached = tok["input_tokens"] - tok["cached_input_tokens"]
    return (uncached * p["in"] + tok["cached_input_tokens"] * p["cached"]
            + tok["output_tokens"] * p["out"]) / 1e6


def bucket_cost(bucket_, table):
    return sum(cost_of(md, mm, table) for mm, md in bucket_["by_model"].items())


def fmt_tok(n):
    for u, v in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= v:
            return f"{n / v:.2f}{u}"
    return str(n)


def table(headers, rows, aligns=None):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    aligns = aligns or ["<"] * len(headers)

    def render(cells):
        return "  ".join(f"{str(c):{a}{w}}" for c, a, w in zip(cells, aligns, widths))
    out = [render(headers), render(["-" * w for w in widths])]
    out += [render(r) for r in rows]
    return "\n".join(out)


def bucket(sessions, keyfn):
    agg = defaultdict(lambda: {k: 0 for k in USAGE_KEYS} | {"sessions": 0, "by_model": defaultdict(lambda: {k: 0 for k in USAGE_KEYS})})
    for s in sessions:
        b = agg[keyfn(s)]
        b["sessions"] += 1
        for k in USAGE_KEYS:
            b[k] += s[k]
            b["by_model"][s["model"]][k] += s[k]
    return agg


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", default="~/.codex/sessions;~/.codex/archived_sessions",
                    help="';'-separated rollout dirs (default: %(default)s)")
    ap.add_argument("--days", type=int, default=0, help="only sessions from the last N days")
    ap.add_argument("--prices", metavar="FILE", help="JSON price overrides (model -> {in,cached,out})")
    ap.add_argument("--top", type=int, default=12, help="projects to show (default: %(default)s)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = ap.parse_args()

    table_prices = dict(PRICE_TABLE)
    if args.prices:
        with open(os.path.expanduser(args.prices)) as f:
            table_prices.update(json.load(f))
    global PRICE_TABLE_USER
    PRICE_TABLE_USER = table_prices

    sessions = load_sessions([r.strip() for r in args.roots.split(";") if r.strip()])
    if args.days:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
        sessions = [s for s in sessions if s["ts"] >= cutoff]
    if not sessions:
        print("no sessions with usage found", file=sys.stderr)
        return 1

    total = {k: sum(s[k] for s in sessions) for k in USAGE_KEYS}
    total["sessions"] = len(sessions)
    by_model = bucket(sessions, lambda s: s["model"])
    by_month = bucket(sessions, lambda s: s["ts"][:7])
    by_project = bucket(sessions, lambda s: s["cwd"])
    by_client = bucket(sessions, lambda s: s["origin"])

    grand_cost = sum(cost_of(d, m, table_prices) for m, d in by_model.items())

    if args.json:
        def slim(agg):
            return {k: {kk: vv for kk, vv in v.items() if kk != "by_model"}
                    | {"cost_usd": round(sum(cost_of(md, mm, table_prices)
                                             for mm, md in v["by_model"].items()), 2)}
                    for k, v in agg.items()}
        print(json.dumps({
            "total": total | {"cost_usd": round(grand_cost, 2),
                              "avg_price_per_mtok": round(grand_cost / (total["total_tokens"] / 1e6), 3) if total["total_tokens"] else 0},
            "by_model": slim(by_model), "by_month": slim(by_month),
            "by_project": slim(by_project), "by_client": slim(by_client),
        }, indent=2))
        return 0

    def model_rows(agg):
        rows = []
        for m in sorted(agg, key=lambda m: -agg[m]["total_tokens"]):
            d = agg[m]
            p, known = price_of(m, table_prices)
            p = p or DEFAULT_PRICE
            cost = cost_of(d, m, table_prices)
            avg = cost / (d["total_tokens"] / 1e6) if d["total_tokens"] else 0
            flag = "" if known else "*"
            rows.append([f"{m}{flag}", d["sessions"], fmt_tok(d["total_tokens"]),
                         f"{100 * d['cached_input_tokens'] / max(d['input_tokens'], 1):.0f}%",
                         fmt_tok(d["output_tokens"]),
                         f"${cost:,.2f}", f"${avg:.2f}",
                         f"${p['in']:.2f}/${p['cached']:.2f}/${p['out']:.2f}"])
        return rows

    hdr = ["key", "sessions", "tokens", "cache%", "out", "cost", "avg $/Mtok", "rate in/cache/out"]
    al = ["<", ">", ">", ">", ">", ">", ">", ">"]

    print(f"CODEX USAGE — {total['sessions']} sessions, "
          f"{min(s['ts'] for s in sessions)[:10]} -> {max(s['ts'] for s in sessions)[:10]}"
          + (f" (last {args.days} days)" if args.days else ""))
    print(f"TOTAL: {fmt_tok(total['input_tokens'])} in ({fmt_tok(total['cached_input_tokens'])} cached, "
          f"{100 * total['cached_input_tokens'] / max(total['input_tokens'], 1):.1f}%) | "
          f"{fmt_tok(total['output_tokens'])} out ({fmt_tok(total['reasoning_output_tokens'])} reasoning) | "
          f"{fmt_tok(total['total_tokens'])} total | "
          f"${grand_cost:,.2f} API-equivalent | "
          f"avg ${grand_cost / (total['total_tokens'] / 1e6):.2f}/Mtok\n")

    print("=== BY MODEL ===")
    print(table(hdr, model_rows(by_model), al))

    print("\n=== BY MONTH ===")
    rows = []
    for mo in sorted(by_month):
        d = by_month[mo]
        cost = bucket_cost(d, table_prices)
        avg = cost / (d["total_tokens"] / 1e6) if d["total_tokens"] else 0
        rows.append([mo, d["sessions"], fmt_tok(d["total_tokens"]), "", fmt_tok(d["output_tokens"]),
                     f"${cost:,.2f}", f"${avg:.2f}", ""])
    print(table(hdr, rows, al))

    print(f"\n=== TOP PROJECTS (top {args.top}) ===")
    top = sorted(by_project, key=lambda p: -by_project[p]["total_tokens"])[:args.top]
    rows = []
    for p in sorted(top, key=lambda p: -by_project[p]["total_tokens"]):
        d = by_project[p]
        cost = bucket_cost(d, table_prices)
        avg = cost / (d["total_tokens"] / 1e6) if d["total_tokens"] else 0
        rows.append([p, d["sessions"], fmt_tok(d["total_tokens"]), "", fmt_tok(d["output_tokens"]),
                     f"${cost:,.2f}", f"${avg:.2f}", ""])
    print(table(hdr, rows, al))

    print("\n=== BY CLIENT ===")
    rows = []
    for o in sorted(by_client, key=lambda o: -by_client[o]["total_tokens"]):
        d = by_client[o]
        cost = bucket_cost(d, table_prices)
        avg = cost / (d["total_tokens"] / 1e6) if d["total_tokens"] else 0
        rows.append([o, d["sessions"], fmt_tok(d["total_tokens"]), "", fmt_tok(d["output_tokens"]),
                     f"${cost:,.2f}", f"${avg:.2f}", ""])
    print(table(hdr, rows, al))

    unknown = sorted(m for m in by_model if m not in table_prices)
    if unknown:
        print(f"\n* priced with default rate: {', '.join(unknown)} "
              f"(override via --prices)")
    print("\nRates: public API rate card (Aug 2026), USD/1M tokens. ChatGPT-plan usage "
          "is credit-based; this is API-equivalent, not billed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
