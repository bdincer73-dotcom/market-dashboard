"""
Renders the single-file HTML dashboard and an 8am-brief-style plaintext
summary. Writes to public/latest.html (always overwritten) and
public/archive/<date>.html (immutable, one per run) so every historical
dashboard stays reproducible per the audit-trail quality gate.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"
PUBLIC_DIR = ROOT / "public"


def render_html(context: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("dashboard.html.jinja")
    return template.render(**context)


def publish(context: dict, run_type: str, run_date: str) -> dict:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    archive_dir = PUBLIC_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    html = render_html(context)

    latest_path = PUBLIC_DIR / "latest.html"
    latest_path.write_text(html, encoding="utf-8")

    # Keyed by run_id (not just date) so a manual re-run via workflow_dispatch
    # never overwrites an earlier run's archived dashboard the same day.
    archive_path = archive_dir / f"{run_date}-{run_type}-{context['run_id']}.html"
    archive_path.write_text(html, encoding="utf-8")

    summary_path = PUBLIC_DIR / "latest_summary.txt"
    summary_path.write_text(render_summary(context, run_type, run_date), encoding="utf-8")

    return {"latest": str(latest_path), "archive": str(archive_path), "summary": str(summary_path)}


def render_summary(context: dict, run_type: str, run_date: str) -> str:
    lines = [
        f"MARKET DASHBOARD — {run_type.upper()} — {run_date}",
        f"Regime: {context['regime']} ({context['regime_label']})",
        "",
        "Why:",
    ]
    for r in context["reasons"]:
        lines.append(f"  - {r}")
    lines.append("")
    lines.append("Data health:")
    for m in context["module_health"]:
        lines.append(f"  - {m['module']}: {m['status']} (obs {m['observation_date'] or '—'})")
    return "\n".join(lines)


REGIME_LABELS = {
    "GREEN": "normal risk",
    "AMBER": "selective",
    "RED": "protect cash",
}
