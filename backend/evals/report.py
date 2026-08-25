"""Writes reports/report.md - a human-readable Markdown test report covering
every scenario run: what it targeted, the full transcript, the metric used,
and the resulting score against its threshold.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

REPORT_DIR = Path(__file__).resolve().parent / "reports"

_INTRO = (
    "Phase 1 evaluates the CrewAI agentic reasoning layer (Intent Router + "
    "Module A/B/C) as a black box. Each scenario is replayed against the "
    "already-running backend over `POST /webhook/whatsapp/sync` - exactly "
    "the same entry point the WhatsApp gateway uses - and the resulting "
    "conversation transcript is scored with "
    "[Ragas agent metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/agents/) "
    "using an LLM judge. **No application code was modified to run these "
    "tests** - this harness only sends HTTP requests and reads back reply "
    "text the orchestrator already produces."
)

_NOT_COVERED = (
    "**Not covered in this Phase 1 run:** Tool Call Accuracy. That metric "
    "needs the actual sequence of tool calls (name + args) each agent made, "
    "which this black-box harness cannot observe from the HTTP reply alone. "
    "Capturing it would need a small, opt-in, default-off instrumentation "
    "hook in `crews/_base.py` (Phase 2) - not added here so no existing "
    "code path changes."
)


def write_report(results: list[dict]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = REPORT_DIR / "report.md"

    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    lines: list[str] = []
    lines.append("# Ragas Agentic Evaluation Report — Phase 1")
    lines.append("")
    lines.append(f"**Run date:** {ts}  ")
    lines.append(f"**Scenarios run:** {total}  ")
    lines.append(f"**Passed:** {passed}/{total}  ")
    lines.append("")
    lines.append(_INTRO)
    lines.append("")
    lines.append(_NOT_COVERED)
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Scenario | Module | Metric | Score | Threshold | Result |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        s = r["scenario"]
        lines.append(
            f"| `{s['id']}` | {s['module']} | {r['metric_label']} | "
            f"{r['score']:.3f} | {r['threshold']:.2f} | "
            f"{'PASS' if r['passed'] else 'FAIL'} |"
        )
    lines.append("")

    lines.append("## Scenario details")

    for r in results:
        s = r["scenario"]
        lines.append("")
        lines.append(f"### `{s['id']}`")
        lines.append("")
        lines.append(f"- **Module under test:** {s['module']}")
        lines.append(f"- **Metric:** {r['metric_label']}")
        lines.append(f"- **Target:** {' '.join(s['description'].split())}")
        if s["metric"] == "topic_adherence":
            lines.append("- **Allowed topics:**")
            for topic in s["allowed_topics"]:
                lines.append(f"  - {topic}")
        elif s["metric"] == "goal_accuracy":
            lines.append(f"- **Reference goal:** {' '.join(s['reference'].split())}")
        elif s["metric"] == "aspect_critic":
            lines.append(f"- **Criterion:** {' '.join(s['definition'].split())}")
            lines.append(f"- **Strictness:** {s.get('strictness', 1)} judge calls, majority vote")
        lines.append(
            f"- **Result:** score `{r['score']:.3f}` vs. threshold "
            f"`{r['threshold']:.2f}` — **{'PASS' if r['passed'] else 'FAIL'}**"
        )
        lines.append("")
        lines.append("**Transcript:**")
        lines.append("")
        if r["transcript"]:
            for t in r["transcript"]:
                speaker = "User" if t.role == "user" else "Assistant"
                lines.append(f"> **{speaker}:** {t.content}")
                lines.append(">")
        else:
            lines.append("> _(scenario failed to run - see logs)_")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
