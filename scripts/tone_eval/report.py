"""Markdown + console report writers for the tone eval harness."""

from __future__ import annotations


def _badge(m: dict) -> str:
    """Compact one-line metric badge for a single output."""
    p = m["passes"]
    def mark(k):
        return "✓" if p.get(k) else "✗"
    parts = [
        f"retention={m['word_retention']} {mark('retention')}",
        f"order={m['order_lcs_ratio']} {mark('order_lcs')}",
        f"len={m['length_ratio']} {mark('len_ratio')}",
    ]
    if "dev_term_preservation" in p:
        parts.append(f"dev_terms={m['dev_term_preservation']} {mark('dev_term_preservation')}")
    if "prof_slang_removal" in p:
        parts.append(f"slang_left={len(m['slang_remaining'])} {mark('prof_slang_removal')}")
        parts.append(f"caps={m['sentence_start_caps_ratio']} {mark('prof_caps')}")
    parts.append(f"guide={m['guide_score']}")
    return " | ".join(parts)


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
    lines.append("| tone | n | retention | order_lcs | len_ratio | guide | latency(s) |")
    lines.append("|------|---|-----------|-----------|-----------|-------|------------|")
    for tone, s in payload["summary"].items():
        lines.append(
            f"| {tone} | {s['n']} | {s['mean_word_retention']} | {s['mean_order_lcs']} "
            f"| {s['mean_length_ratio']} | {s['mean_guide_score']} | {s['mean_latency_s']} |"
        )
    lines.append("")
    lines.append("### Gate pass-rates")
    for tone, s in payload["summary"].items():
        lines.append(f"- **{tone}**: {s['gate_pass_rates']}")
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
            f"{tone:<14}{s['mean_word_retention']:>7}{s['mean_order_lcs']:>7}"
            f"{s['mean_length_ratio']:>7}{s['mean_guide_score']:>7}{s['mean_latency_s']:>8}"
        )
    print("==================================================\n")
