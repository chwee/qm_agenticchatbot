"""CLI entry point for the Phase 1 Ragas agentic evaluation.

Usage:
    cd backend/evals
    pip install -r requirements-eval.txt
    python run_eval.py [--base-url http://localhost:8000] [--model gpt-4o-mini]

Prerequisites:
    - The backend (Terminal 2 in the main README) must already be running.
    - OPENAI_API_KEY must be set in the environment - used only by the eval
      judge LLM here, separate from the app's own OPENAI_MODEL.

This script never imports from app/ and never touches the database directly:
it drives the backend purely over HTTP via client.SyncClient, and writes its
findings to reports/report.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the same OPENAI_API_KEY the backend itself uses (backend/.env), so the
# judge LLM here doesn't require a separate credential to be configured. This
# only affects this eval process's environment - it does not touch app/.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from client import SyncClient  # noqa: E402
from metrics import build_evaluator_llm, score  # noqa: E402
from report import write_report  # noqa: E402
from runner import run_scenario  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("evals")

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"

METRIC_LABELS = {
    "topic_adherence": "Topic Adherence",
    "goal_accuracy": "Agent Goal Accuracy",
    "aspect_critic": "Aspect Critic (refusal check)",
}


def load_scenarios() -> list[dict]:
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Ragas Phase 1 agentic evaluation")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Backend base URL")
    parser.add_argument("--model", default="gpt-4o-mini", help="Judge LLM used by Ragas to score transcripts")
    args = parser.parse_args()

    scenarios = load_scenarios()
    if not scenarios:
        log.error("No scenarios found in %s", SCENARIOS_DIR)
        sys.exit(1)

    client = SyncClient(base_url=args.base_url)
    evaluator_llm = build_evaluator_llm(args.model)

    results: list[dict] = []
    for scenario in scenarios:
        log.info("Running scenario: %s", scenario["id"])
        threshold = scenario.get("threshold", 0.8)
        try:
            sr = run_scenario(client, scenario)
            s = score(sr, evaluator_llm)
            results.append({
                "scenario": scenario,
                "transcript": sr.transcript,
                "metric_label": METRIC_LABELS.get(scenario["metric"], scenario["metric"]),
                "score": s,
                "threshold": threshold,
                "passed": s >= threshold,
            })
            log.info("  -> score=%.3f (threshold %.2f)", s, threshold)
        except Exception:
            log.exception("Scenario %s failed to run", scenario["id"])
            results.append({
                "scenario": scenario,
                "transcript": [],
                "metric_label": METRIC_LABELS.get(scenario["metric"], scenario["metric"]),
                "score": 0.0,
                "threshold": threshold,
                "passed": False,
            })

    client.close()

    report_path = write_report(results)
    log.info("Report written to %s", report_path)

    failed = [r for r in results if not r["passed"]]
    if failed:
        log.warning("%d/%d scenarios failed", len(failed), len(results))
        sys.exit(1)


if __name__ == "__main__":
    main()
