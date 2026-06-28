#!/usr/bin/env python3
"""Generate a standalone HTML health report for local SDLC agent evals."""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGES = ["spec-agent", "test-agent", "builder-agent", "verifier-agent", "eval-agent"]
STAGE_LABELS = {
    "spec-agent": "Spec",
    "test-agent": "Test",
    "builder-agent": "Builder",
    "verifier-agent": "Verifier",
    "eval-agent": "Eval",
}
SELECTED_MODEL_PATHS = {
    "spec-agent": Path(".workflow/agents/spec/selected_model.json"),
    "test-agent": Path(".workflow/agents/test/selected_model.json"),
    "builder-agent": Path(".workflow/agents/builder/selected_model.json"),
    "verifier-agent": Path(".workflow/agents/verifier/selected_model.json"),
    "eval-agent": Path(".workflow/agents/eval/selected_model.json"),
}
STALE_DAYS = 7
HIGH_COST_USD = 0.25
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
SNAPSHOT_MAX_ENTRIES = 240
SNAPSHOT_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "playwright-report",
    "target",
    "test-results",
    "vendor",
}


@dataclass
class EvalReport:
    stage: str
    path: Path
    run_id: str
    timestamp: datetime | None
    data: dict[str, Any]


def main() -> int:
    args = parse_args()
    reports = discover_reports(args.eval_root)
    selected_models = load_selected_models()
    latest = latest_reports_by_stage(reports)
    warnings = health_warnings(latest, selected_models, datetime.now(timezone.utc))
    status = overall_status(latest, selected_models)

    if args.validate:
        snapshot_count = sum(1 for report in latest.values() for attempt in report.data.get("attempts", []) if attempt_root(report, attempt))
        print(f"reports: {len(reports)}")
        print(f"latest_stages: {', '.join(stage for stage in STAGES if stage in latest)}")
        print(f"selected_models: {sum(1 for item in selected_models.values() if item)}")
        print(f"overall_status: {status}")
        print(f"warnings: {len(warnings)}")
        print(f"filesystem_snapshots: {snapshot_count}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(reports, latest, selected_models, warnings, status), encoding="utf-8")
    print(f"wrote: {args.output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-root", type=Path, default=Path(".workflow/eval-runs"))
    parser.add_argument("--output", type=Path, default=Path(".workflow/eval-runs/report.html"))
    parser.add_argument("--validate", action="store_true", help="Scan inputs and print a summary without writing HTML")
    return parser.parse_args()


def discover_reports(eval_root: Path) -> list[EvalReport]:
    reports = []
    for path in sorted(eval_root.glob("**/report.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stage = stage_from_path(eval_root, path)
        run_id = run_id_from_path(path)
        reports.append(EvalReport(stage=stage, path=path, run_id=run_id, timestamp=parse_run_timestamp(run_id), data=data))
    return sorted(reports, key=lambda report: (report.stage, report.timestamp or datetime.min.replace(tzinfo=timezone.utc), report.path.as_posix()))


def stage_from_path(eval_root: Path, report_path: Path) -> str:
    relative = report_path.relative_to(eval_root)
    parts = relative.parts
    if len(parts) >= 3 and parts[0] in STAGES:
        return parts[0]
    return "spec-agent"


def run_id_from_path(report_path: Path) -> str:
    return report_path.parent.name


def parse_run_timestamp(run_id: str) -> datetime | None:
    if not TIMESTAMP_RE.match(run_id):
        return None
    return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def latest_reports_by_stage(reports: list[EvalReport]) -> dict[str, EvalReport]:
    latest: dict[str, EvalReport] = {}
    for report in reports:
        current = latest.get(report.stage)
        if current is None or report_sort_key(report) > report_sort_key(current):
            latest[report.stage] = report
    return latest


def report_sort_key(report: EvalReport) -> tuple[datetime, str]:
    return (report.timestamp or datetime.min.replace(tzinfo=timezone.utc), report.path.as_posix())


def load_selected_models() -> dict[str, dict[str, Any] | None]:
    selected: dict[str, dict[str, Any] | None] = {}
    for stage, path in SELECTED_MODEL_PATHS.items():
        if not path.exists():
            selected[stage] = None
            continue
        try:
            selected[stage] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            selected[stage] = None
    return selected


def health_warnings(
    latest: dict[str, EvalReport],
    selected_models: dict[str, dict[str, Any] | None],
    now: datetime,
) -> list[str]:
    warnings = []
    for stage in STAGES:
        label = STAGE_LABELS[stage]
        report = latest.get(stage)
        selected = selected_models.get(stage)
        if selected is None:
            warnings.append(f"{label}: missing selected model")
        if report is None:
            warnings.append(f"{label}: no eval report found")
            continue
        if report.data.get("status") == "fail":
            warnings.append(f"{label}: latest eval failed")
        if report.timestamp and (now - report.timestamp).days > STALE_DAYS:
            warnings.append(f"{label}: latest eval is older than {STALE_DAYS} days")
        if float(report.data.get("total_cost") or 0) > HIGH_COST_USD:
            warnings.append(f"{label}: latest eval cost exceeded ${HIGH_COST_USD:.2f}")
        artifact_findings = latest_artifact_findings(report)
        if artifact_findings:
            warnings.append(f"{label}: artifact policy findings present")
    return warnings


def latest_artifact_findings(report: EvalReport) -> list[str]:
    findings = []
    for attempt in report.data.get("attempts", []):
        artifact_policy = attempt.get("artifact_policy") or {}
        findings.extend(artifact_policy.get("findings") or [])
    return findings


def overall_status(latest: dict[str, EvalReport], selected_models: dict[str, dict[str, Any] | None]) -> str:
    if any(report.data.get("status") == "fail" for report in latest.values()):
        return "fail"
    if any(stage not in latest for stage in STAGES):
        return "warn"
    if any(selected_models.get(stage) is None for stage in STAGES):
        return "warn"
    return "pass"


def render_html(
    reports: list[EvalReport],
    latest: dict[str, EvalReport],
    selected_models: dict[str, dict[str, Any] | None],
    warnings: list[str],
    status: str,
) -> str:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    latest_cost = sum(float(report.data.get("total_cost") or 0) for report in latest.values())
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hooky SDLC Pipeline Health</title>
  <style>{css()}</style>
</head>
<body>
  <header>
    <div>
      <p class="eyebrow">Hooky SDLC</p>
      <h1>Pipeline Health</h1>
      <p class="muted">Generated {escape(generated_at)} from {len(reports)} eval reports.</p>
    </div>
    <div class="status status-{escape(status)}">{escape(status.upper())}</div>
  </header>
  <main>
    <section class="summary-grid">
      <div class="metric"><span>Stages With Reports</span><strong>{len(latest)} / {len(STAGES)}</strong></div>
      <div class="metric"><span>Selected Models</span><strong>{sum(1 for item in selected_models.values() if item)} / {len(STAGES)}</strong></div>
      <div class="metric"><span>Latest Eval Cost</span><strong>${latest_cost:.5f}</strong></div>
      <div class="metric"><span>Warnings</span><strong>{len(warnings)}</strong></div>
    </section>
    {render_warnings(warnings)}
    {render_stage_cards(latest)}
    {render_selected_models(selected_models)}
    {render_attempts(latest)}
  </main>
</body>
</html>
"""


def render_warnings(warnings: list[str]) -> str:
    if not warnings:
        return '<section class="panel"><h2>Health Warnings</h2><p class="empty">No health warnings.</p></section>'
    items = "".join(f"<li>{escape(item)}</li>" for item in warnings)
    return f'<section class="panel warning-panel"><h2>Health Warnings</h2><ul>{items}</ul></section>'


def render_stage_cards(latest: dict[str, EvalReport]) -> str:
    cards = []
    for stage in STAGES:
        report = latest.get(stage)
        if not report:
            cards.append(f"""
      <article class="stage-card missing">
        <h3>{escape(STAGE_LABELS[stage])}</h3>
        <p class="status status-warn">NO REPORT</p>
        <p class="muted">No eval report found.</p>
      </article>""")
            continue
        attempts = report.data.get("attempts") or []
        cards.append(f"""
      <article class="stage-card">
        <h3>{escape(STAGE_LABELS[stage])}</h3>
        <p class="status status-{escape(str(report.data.get('status', 'warn')))}">{escape(str(report.data.get('status', 'unknown')).upper())}</p>
        <dl>
          <dt>Winner</dt><dd>{escape(report.data.get("winner_model") or "None")}</dd>
          <dt>Attempts</dt><dd>{len(attempts)}</dd>
          <dt>Cost</dt><dd>${float(report.data.get("total_cost") or 0):.5f}</dd>
          <dt>Run</dt><dd><code>{escape(report.run_id)}</code></dd>
          <dt>Report</dt><dd><code>{escape(report.path.as_posix())}</code></dd>
        </dl>
      </article>""")
    return f'<section class="panel"><h2>Latest Stage Status</h2><div class="stage-grid">{"".join(cards)}</div></section>'


def render_selected_models(selected_models: dict[str, dict[str, Any] | None]) -> str:
    rows = []
    for stage in STAGES:
        selected = selected_models.get(stage)
        if not selected:
            rows.append(f"<tr><td>{escape(STAGE_LABELS[stage])}</td><td colspan='6' class='muted'>Missing selected model</td></tr>")
            continue
        rows.append(
            "<tr>"
            f"<td>{escape(STAGE_LABELS[stage])}</td>"
            f"<td><code>{escape(str(selected.get('variant_id') or selected.get('model') or ''))}</code></td>"
            f"<td>{escape(json_short(selected.get('reasoning_request')))}</td>"
            f"<td>{escape(str(selected.get('source') or ''))}</td>"
            f"<td>${format_optional_float(selected.get('actual_cost'))}</td>"
            f"<td>{escape(str(selected.get('updated_at') or ''))}</td>"
            f"<td><code>{escape(str(selected.get('report') or ''))}</code></td>"
            "</tr>"
        )
    return f"""
    <section class="panel">
      <h2>Selected Models</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Stage</th><th>Model Variant</th><th>Reasoning</th><th>Source</th><th>Actual Cost</th><th>Updated</th><th>Source Report</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>"""


def render_attempts(latest: dict[str, EvalReport]) -> str:
    sections = []
    for stage in STAGES:
        report = latest.get(stage)
        if not report:
            continue
        attempt_details = "".join(render_attempt(report, attempt, index) for index, attempt in enumerate(report.data.get("attempts") or [], 1))
        sections.append(f"""
      <details class="attempt-stage">
        <summary>{escape(STAGE_LABELS[stage])} Attempts ({len(report.data.get("attempts") or [])})</summary>
        {attempt_details or '<p class="empty">No attempts recorded.</p>'}
      </details>""")
    return f'<section class="panel"><h2>Attempt Drilldown</h2>{"".join(sections)}</section>'


def render_attempt(report: EvalReport, attempt: dict[str, Any], index: int) -> str:
    deterministic = attempt.get("deterministic") or {}
    judge = attempt.get("judge") or {}
    tool_use = attempt.get("tool_use") or {}
    artifact_policy = attempt.get("artifact_policy") or {}
    findings = list(deterministic.get("findings") or [])
    findings.extend(artifact_policy.get("findings") or [])
    findings.extend(judge.get("critical_findings") or [])
    findings.extend(judge.get("findings") or [])
    cases = attempt.get("cases") or []
    return f"""
        <details class="attempt">
          <summary>
            <span>#{index} <code>{escape(str(attempt.get("variant_id") or attempt.get("model") or "unknown"))}</code></span>
            <span class="status status-{escape(str(attempt.get('status', 'warn')))}">{escape(str(attempt.get('status', 'unknown')).upper())}</span>
          </summary>
          <div class="attempt-body">
            <dl class="attempt-meta">
              <dt>Estimated cost</dt><dd>${format_optional_float(attempt.get("estimated_cost"))}</dd>
              <dt>Actual cost</dt><dd>${format_optional_float(attempt.get("cost"))}</dd>
              <dt>Cached</dt><dd>{escape(str(attempt.get("cached", False)))}</dd>
              <dt>Tool calls</dt><dd>{escape(str(tool_use.get("tool_calls", 0)))}</dd>
              <dt>Compactions</dt><dd>{escape(str(tool_use.get("compactions", 0)))}</dd>
              <dt>Judge</dt><dd>{escape(str(judge.get("status") or "not run"))}</dd>
              <dt>Artifact dir</dt><dd><code>{escape(str(attempt.get("artifact_dir") or ""))}</code></dd>
            </dl>
            {render_tools(tool_use)}
            {render_scores(judge.get("scores") or {})}
            {render_findings(findings)}
            {render_cases(cases)}
            {render_filesystem_snapshot(report, attempt)}
          </div>
        </details>"""


def render_tools(tool_use: dict[str, Any]) -> str:
    tools = tool_use.get("tools") or {}
    if not tools:
        return '<p class="muted">No tool usage recorded.</p>'
    items = "".join(f"<li><code>{escape(str(name))}</code>: {escape(str(count))}</li>" for name, count in sorted(tools.items()))
    return f"<h4>Tools</h4><ul class='compact-list'>{items}</ul>"


def render_scores(scores: dict[str, Any]) -> str:
    if not scores:
        return ""
    items = "".join(f"<li>{escape(str(name))}: <strong>{escape(str(value))}</strong></li>" for name, value in sorted(scores.items()))
    return f"<h4>Judge Scores</h4><ul class='compact-list'>{items}</ul>"


def render_findings(findings: list[str]) -> str:
    if not findings:
        return '<p class="empty">No findings.</p>'
    items = "".join(f"<li>{escape(str(item))}</li>" for item in findings)
    return f"<h4>Findings</h4><ul>{items}</ul>"


def render_cases(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return ""
    rows = []
    for case in cases:
        rows.append(
            "<tr>"
            f"<td>{escape(str(case.get('name') or ''))}</td>"
            f"<td>{escape(str(case.get('expected_status') or ''))}</td>"
            f"<td>{escape(str(case.get('actual_status') or ''))}</td>"
            f"<td>{escape(str(case.get('status') or ''))}</td>"
            f"<td>{escape('; '.join(str(item) for item in case.get('findings', [])))}</td>"
            "</tr>"
        )
    return f"""
      <h4>Cases</h4>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Expected</th><th>Actual</th><th>Eval</th><th>Findings</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>"""


def render_filesystem_snapshot(report: EvalReport, attempt: dict[str, Any]) -> str:
    root = attempt_root(report, attempt)
    if root is None:
        return '<h4>Filesystem After Run</h4><p class="muted">No working folder found for this attempt.</p>'
    snapshot = filesystem_snapshot(root)
    rows = []
    for entry in snapshot["entries"]:
        name = entry["path"].split("/")[-1] or entry["path"]
        indent = max(0, entry["depth"]) * 18
        marker = "/" if entry["type"] == "dir" else ""
        rows.append(
            "<tr>"
            f"<td class='tree-path' style='padding-left:{indent + 10}px'>{escape(name + marker)}</td>"
            f"<td>{escape(entry['type'])}</td>"
            f"<td>{escape(format_bytes(entry.get('size')))}</td>"
            f"<td><code>{escape(entry['path'])}</code></td>"
            "</tr>"
        )
    truncated = ""
    if snapshot["truncated"]:
        truncated = f"<p class='muted'>Snapshot truncated after {SNAPSHOT_MAX_ENTRIES} entries. Excluded noisy folders: {escape(', '.join(sorted(SNAPSHOT_EXCLUDED_DIRS)))}.</p>"
    return f"""
      <h4>Filesystem After Run</h4>
      <p class="muted">Root: <code>{escape(root.as_posix())}</code> ({snapshot['file_count']} files, {snapshot['dir_count']} directories)</p>
      {truncated}
      <div class="tree-wrap">
        <table class="tree-table">
          <thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Path</th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan="4" class="muted">No files found.</td></tr>'}</tbody>
        </table>
      </div>"""


def attempt_root(report: EvalReport, attempt: dict[str, Any]) -> Path | None:
    variant = str(attempt.get("variant_id") or attempt.get("model") or "")
    if variant:
        candidate = report.path.parent / safe_name(variant)
        if candidate.exists() and candidate.is_dir():
            return candidate
    artifact_dir = attempt.get("artifact_dir")
    if not artifact_dir:
        return None
    path = Path(str(artifact_dir))
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return None
    run_dir = report.path.parent.resolve()
    current = path.resolve()
    while current != current.parent:
        if current.parent == run_dir:
            return current
        current = current.parent
    return path if path.is_dir() else path.parent


def filesystem_snapshot(root: Path) -> dict[str, Any]:
    entries = []
    file_count = 0
    dir_count = 0
    truncated = False
    for path in sorted(root.rglob("*")):
        relative_parts = path.relative_to(root).parts
        if any(part in SNAPSHOT_EXCLUDED_DIRS for part in relative_parts):
            continue
        if path.is_dir():
            dir_count += 1
        elif path.is_file():
            file_count += 1
        if len(entries) >= SNAPSHOT_MAX_ENTRIES:
            truncated = True
            continue
        if path.is_dir():
            entry_type = "dir"
            size = None
        elif path.is_file():
            entry_type = "file"
            size = path.stat().st_size
        else:
            continue
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "type": entry_type,
                "size": size,
                "depth": len(relative_parts) - 1,
            }
        )
    return {
        "entries": entries,
        "file_count": file_count,
        "dir_count": dir_count,
        "truncated": truncated,
    }


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-")


def format_bytes(value: Any) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def format_optional_float(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.5f}"
    except (TypeError, ValueError):
        return str(value)


def json_short(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    return json.dumps(value, sort_keys=True)


def escape(value: str) -> str:
    return html.escape(value, quote=True)


def css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --text: #17202a;
  --muted: #667085;
  --line: #d8dee8;
  --pass: #147d4f;
  --warn: #a15c00;
  --fail: #b42318;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding: 28px 32px 10px; max-width: 1440px; margin: 0 auto; }
h1 { margin: 0; font-size: 32px; letter-spacing: 0; }
h2 { margin: 0 0 16px; font-size: 18px; }
h3 { margin: 0 0 10px; font-size: 16px; }
h4 { margin: 18px 0 8px; font-size: 13px; text-transform: uppercase; color: var(--muted); }
main { max-width: 1440px; margin: 0 auto; padding: 18px 32px 40px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid var(--line); padding: 9px 10px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
dl { display: grid; grid-template-columns: 92px 1fr; gap: 6px 10px; margin: 0; }
dt { color: var(--muted); }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
summary { cursor: pointer; }
.eyebrow { margin: 0 0 4px; color: var(--muted); text-transform: uppercase; font-size: 12px; font-weight: 700; }
.muted { color: var(--muted); }
.empty { color: var(--muted); margin: 8px 0 0; }
.panel, .metric, .stage-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.panel { padding: 18px; margin: 18px 0; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.metric { padding: 16px; }
.metric span { display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; }
.metric strong { display: block; margin-top: 6px; font-size: 24px; }
.stage-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; }
.stage-card { padding: 14px; min-width: 0; }
.stage-card.missing { opacity: .8; }
.status { display: inline-flex; align-items: center; justify-content: center; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; border: 1px solid currentColor; }
.status-pass { color: var(--pass); }
.status-warn, .status-running, .status-unknown { color: var(--warn); }
.status-fail { color: var(--fail); }
.warning-panel { border-color: #f3b27b; background: #fff8f0; }
.table-wrap { overflow-x: auto; }
.attempt-stage { border-top: 1px solid var(--line); padding: 12px 0; }
.attempt { border: 1px solid var(--line); border-radius: 8px; margin: 10px 0; padding: 10px 12px; background: #fbfcfe; }
.attempt > summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.attempt-body { padding-top: 12px; }
.attempt-meta { grid-template-columns: 120px 1fr; }
.compact-list { display: flex; flex-wrap: wrap; gap: 8px 16px; padding-left: 18px; }
.tree-wrap { max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
.tree-table th { position: sticky; top: 0; background: #fff; z-index: 1; }
.tree-path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; white-space: nowrap; }
@media (max-width: 980px) {
  header { flex-direction: column; }
  .summary-grid, .stage-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 640px) {
  main, header { padding-left: 16px; padding-right: 16px; }
  .summary-grid, .stage-grid { grid-template-columns: 1fr; }
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
