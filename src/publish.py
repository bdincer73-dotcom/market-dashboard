"""
Renders the single-file HTML dashboard and an 8am-brief-style plaintext
summary. Writes to docs/latest.html and docs/index.html (always overwritten,
identical content) plus docs/archive/<date>.html (immutable, one per run) so
every historical dashboard stays reproducible per the audit-trail quality
gate.

Output lives in docs/ (not public/) specifically so GitHub Pages can serve it
straight from the main branch without a separate build step - Pages only
supports the repo root or a /docs folder for branch-based deployment, so
index.html is what resolves at the bare Pages URL.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"
PUBLIC_DIR = ROOT / "docs"


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

    # index.html is what GitHub Pages serves at the bare site URL.
    index_path = PUBLIC_DIR / "index.html"
    index_path.write_text(html, encoding="utf-8")

    # Keyed by run_id (not just date) so a manual re-run via workflow_dispatch
    # never overwrites an earlier run's archived dashboard the same day.
    archive_path = archive_dir / f"{run_date}-{run_type}-{context['run_id']}.html"
    archive_path.write_text(html, encoding="utf-8")

    summary_path = PUBLIC_DIR / "latest_summary.txt"
    summary_path.write_text(render_summary(context, run_type, run_date), encoding="utf-8")

    return {
        "latest": str(latest_path),
        "index": str(index_path),
        "archive": str(archive_path),
        "summary": str(summary_path),
    }


def render_summary(context: dict, run_type: str, run_date: str) -> str:
    lines = [
        f"MARKET DASHBOARD — {run_type.upper()} — {run_date}",
        f"Regime: {context['regime']} ({context['regime_label']})",
        "",
        "Why:",
    ]
    for r in context["reasons"]:
        lines.append(f"  - {r}")

    if context.get("bear_indicator") is not None:
        lines.append("")
        if context["bear_indicator"].status != "FAILED":
            lines.append(f"Bear Indicator: {context['bear_signal']} ({context['bear_score']}/8)")
            for r in context["bear_reasons"]:
                lines.append(f"  - {r}")
        else:
            lines.append(f"Bear Indicator: FAILED - {context['bear_indicator'].notes}")

    candidates = context.get("candidates")
    if candidates is not None:
        lines.append("")
        lines.append(
            f"CSP candidates: {candidates['candidate_count']} pass all gates, "
            f"{candidates['watch_only_count']} watch-only"
        )
        for c in candidates["candidates"]:
            lines.append(
                f"  - {c['ticker']} ${c['pick']['strike']} {c['pick']['expiry']} "
                f"({c['pick']['dte']}D) -> {c['pick']['annualized_return_pct']}% ann."
            )

    earnings = context.get("earnings")
    if earnings is not None:
        lines.append("")
        if earnings.status != "FAILED":
            lines.append(f"Earnings scheduled: {earnings.payload['scheduled_count']} tickers in next {earnings.payload['lookahead_days']}D")
        else:
            lines.append(f"Earnings: FAILED - {earnings.notes}")

    news = context.get("news")
    if news is not None:
        lines.append("")
        if news.status != "FAILED":
            lines.append(
                f"News: {news.payload['ok_count']} tickers with headlines, "
                f"{news.payload['unavailable_count']} unavailable"
            )
        else:
            lines.append(f"News: FAILED - {news.notes}")

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
