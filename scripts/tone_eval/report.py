"""Markdown + console report writers for the tone eval harness."""

from __future__ import annotations


def _badge(m: dict) -> str:
    """Compact one-line metric badge for a single output."""
    p = m["passes"]
    def mark(k):
        return "✓" if p.get(k) else "✗"
    def num(k):
        """N/A metrics are None (transformative rows omit the guide family)."""
        v = m.get(k)
        return "n/a" if v is None else v

    parts = [
        f"retention={num('word_retention')} {mark('retention')}",
        f"order={num('order_lcs_ratio')} {mark('order_lcs')}",
        f"len={num('length_ratio')} {mark('len_ratio')}",
    ]
    if m.get("required_filler_removal") is not None:
        parts.append(f"filler={m['required_filler_removal']} {mark('required_filler_removal')}")
    if "dev_term_preservation" in p:
        parts.append(f"dev_terms={m['dev_term_preservation']} {mark('dev_term_preservation')}")
    if "prof_slang_removal" in p:
        parts.append(f"slang_left={len(m['slang_remaining'])} {mark('prof_slang_removal')}")
        parts.append(f"caps={m['sentence_start_caps_ratio']} {mark('prof_caps')}")
    parts.append(f"guide={m['guide_score']}")
    return " | ".join(parts)


def _cell(summary: dict, key: str) -> str:
    """Render a summarize() metric as "mean (n)", or n/a when it never applied."""
    entry = summary.get(key) or {}
    mean = entry.get("mean")
    return "n/a" if mean is None else f"{mean} ({entry.get('n', 0)})"


def render_markdown(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Tone eval — {payload['model']}")
    lines.append("")
    lines.append(f"- generated: `{payload['generated']}`")
    lines.append(f"- tones: {', '.join(payload['tones'])}")
    cfg = payload.get("config", {})
    lines.append(f"- config: {cfg}")
    lines.append("")

    # ---- summary table ----
    lines.append("## Summary (per tone)")
    lines.append("")
    # Each cell is "mean (n)" — n is the number of rows the metric APPLIED to,
    # which is not the row count: transformative intensities omit the whole
    # guide-preservation family, so a mean drawn from 2 rows must not read like
    # one drawn from 18.
    lines.append("| tone | n | retention | filler | order_lcs | len_ratio | guide | latency(s) |")
    lines.append("|------|---|-----------|--------|-----------|-----------|-------|------------|")
    for tone, s in payload["summary"].items():
        lines.append(
            f"| {tone} | {s['n']} | {_cell(s, 'word_retention')} "
            f"| {_cell(s, 'required_filler_removal')} | {_cell(s, 'order_lcs')} "
            f"| {_cell(s, 'length_ratio')} | {_cell(s, 'guide_score')} | {s['mean_latency_s']} |"
        )
    lines.append("")
    lines.append("### Gate pass-rates")
    for tone, s in payload["summary"].items():
        rates = ", ".join(
            f"{k}={v['rate']} (n={v['n']})" for k, v in sorted(s["gate_pass_rates"].items())
        )
        lines.append(f"- **{tone}**: {rates or 'no applicable gates'}")
    lines.append("")

    # ---- side-by-side per sample ----
    lines.append("## Outputs (side by side)")
    lines.append("")
    by_sample: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in payload["results"]:
        if r["sample_id"] not in by_sample:
            by_sample[r["sample_id"]] = []
            order.append(r["sample_id"])
        by_sample[r["sample_id"]].append(r)

    for sid in order:
        recs = by_sample[sid]
        stresses = ", ".join(recs[0]["stresses"])
        lines.append(f"### `{sid}`  _(stresses: {stresses})_")
        lines.append("")
        lines.append(f"> **input:** {recs[0]['input']}")
        lines.append("")
        lines.append("| tone | output | metrics |")
        lines.append("|------|--------|---------|")
        for r in recs:
            out = (r["output"] or "").replace("|", "\\|").replace("\n", " ")
            err = f" **[ERROR: {r['error']}]**" if r.get("error") else ""
            badge = _badge(r["metrics"])
            lines.append(f"| {r['tone']} | {out}{err} | {badge} |")
        if "judge" in recs[0]:
            lines.append("")
            for r in recs:
                lines.append(f"  - judge[{r['tone']}]: {r.get('judge')}")
        lines.append("")

    return "\n".join(lines) + "\n"


def print_console_summary(payload: dict) -> None:
    print("\n================ TONE EVAL SUMMARY ================")
    print(f"model: {payload['model']}")
    hdr = f"{'tone':<14}{'reten':>7}{'order':>7}{'len':>7}{'guide':>7}{'lat(s)':>8}"
    print(hdr)
    print("-" * len(hdr))
    for tone, s in payload["summary"].items():
        print(
            f"{tone:<14}{_cell(s, 'word_retention'):>12}{_cell(s, 'order_lcs'):>12}"
            f"{_cell(s, 'length_ratio'):>12}{_cell(s, 'guide_score'):>12}{s['mean_latency_s']:>8}"
        )
    print("==================================================\n")
