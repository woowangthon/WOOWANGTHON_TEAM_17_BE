"""Run the versioned, synthetic-only AI quality protocol."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app.ai_evaluation import MAX_LIVE_CALLS, live_interpretations, live_schedules, offline_report, rescore_interpretations, audit_saved_schedules


def _markdown(report: dict) -> str:
    lines = [
        "# Synthetic AI evaluation",
        "",
        f"- Mode: `{report.get('executionMode')}`",
        f"- Corpus SHA-256: `{report.get('corpusSha256')}`",
        f"- Primary synthetic inputs: {report.get('primaryInputCount', 5)}",
        f"- Synthetic challenge inputs: {report.get('challengeInputCount', 0)}",
        f"- Gemini parsing attempts: {report.get('modelParsingCalls', 0)}",
        f"- Gemini schedule drafting attempts: {report.get('schedulingModelCalls', 0)}",
        f"- Human evaluators: {report.get('humanEvaluation', {}).get('respondents', 0)}",
        "- Satisfaction: **not measured**",
        "",
        "## Results",
        "",
    ]
    for key in ("limitations",):
        for value in report.get(key, []):
            lines.append(f"- {value}")
    metrics = report.get("groundedMetrics") or report.get("parsingMetrics")
    if metrics:
        lines += ["", "- Bounded lexical metrics: " + json.dumps(metrics, ensure_ascii=False, sort_keys=True)]
    if report.get("items"):
        lines += ["", "| Case | Status | TP | FP | FN | Grounded TP/FP/FN |", "|---|---:|---:|---:|---:|---:|"]
        for item in report["items"]:
            raw, grounded = item.get("raw", {}), item.get("grounded", {})
            lines.append(f"| {item.get('caseId')} | {item.get('status')} | {raw.get('truePositive', 0)} | {raw.get('falsePositive', 0)} | {raw.get('falseNegative', 0)} | {grounded.get('truePositive', 0)}/{grounded.get('falsePositive', 0)}/{grounded.get('falseNegative', 0)} |")
    lines.append("")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make one real Gemini call per synthetic primary/challenge/member input; currently 19 calls")
    parser.add_argument("--schedule-from", type=Path, help="replay a saved live parsing JSON; at most five additional Gemini draft calls")
    parser.add_argument("--rescore-from", type=Path, help="re-score saved responses without any provider call")
    parser.add_argument("--audit-from", type=Path, help="independently audit saved synthetic schedule orders without provider calls")
    parser.add_argument("--parsing-from", type=Path, help="saved parsing JSON required with --audit-from")
    parser.add_argument("--max-calls", type=int, default=MAX_LIVE_CALLS)
    parser.add_argument("--output", type=Path, help="output JSON path (Markdown sibling is also written)")
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.live, args.schedule_from, args.rescore_from, args.audit_from)) > 1:
        parser.error("Choose one of --live, --schedule-from, --rescore-from, --audit-from")
    if bool(args.audit_from) != bool(args.parsing_from):
        parser.error("--audit-from and --parsing-from must be specified together")
    if args.live and not 1 <= args.max_calls <= MAX_LIVE_CALLS:
        parser.error(f"--max-calls must be between 1 and {MAX_LIVE_CALLS}")
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path(".local-dev") / "evaluation" / f"report-{now}.json"
    if not output.resolve().is_relative_to((Path(".local-dev") / "evaluation").resolve()):
        parser.error("Evaluation reports must be saved under .local-dev/evaluation")
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.audit_from:
        report = audit_saved_schedules(json.loads(args.audit_from.read_text(encoding="utf-8")),
                                      json.loads(args.parsing_from.read_text(encoding="utf-8")))
    elif args.rescore_from:
        report = rescore_interpretations(json.loads(args.rescore_from.read_text(encoding="utf-8")))
    elif args.schedule_from:
        parser_report = json.loads(args.schedule_from.read_text(encoding="utf-8"))
        report = live_schedules(parser_report, maximum_total_calls=args.max_calls)
    else:
        report = rescore_interpretations(live_interpretations(maximum_calls=args.max_calls)) if args.live else offline_report()
    if args.live:
        corpus_primary = 5
        report["primaryInputCount"] = corpus_primary
        report["challengeInputCount"] = report.get("caseCount", 0) - corpus_primary
        report["humanEvaluation"] = {"respondents": 0, "satisfaction": None, "status": "not measured"}
        report["limitations"] = [
            "Inputs are five fictional personas plus synthetic conflict cases; not a user sample.",
            "Parser calls use this model/configuration once each; no retries.",
            "The lexical gold rubric is deliberately narrow; human semantic adjudication is pending.",
            "Schedule generation was not run by this command.",
        ]
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(f"Evaluation report: {output}")
    print(f"Gemini calls: {report.get('totalLiveCalls', report.get('modelParsingCalls', 0))}; human evaluators: {report.get('humanEvaluation', {}).get('respondents', 0)}")
    return output


if __name__ == "__main__":
    run()
