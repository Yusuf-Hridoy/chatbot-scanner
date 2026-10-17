"""Step 4: render a self-contained HTML report (and optionally a PDF) for a run."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from chatbot_audit.attacks.attack_loader import load_attacks
from chatbot_audit.data_models import Attack, Exchange, Finding, Grade, RunInfo, Verdict
from chatbot_audit.pipeline.grade_calculator import calculate_grade
from chatbot_audit.pipeline.run_folder import (
    FINDINGS_FILE,
    REPLAYS_FILE,
    REPORT_HTML_FILE,
    REPORT_PDF_FILE,
    RUN_INFO_FILE,
    VERDICTS_FILE,
    latest_exchanges,
    read_json,
    read_jsonl,
    read_latest_exchanges,
    require_run_dir,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "report_templates"
RETEST_EMAIL = "retest@auditlab.example"
CATEGORY_NAMES = {
    "prompt_leak": "Hidden instructions stay private",
    "false_promises": "No invented discounts or wrong policies",
    "ai_disclosure": "Discloses that it is an AI",
    "data_leak": "Protects customer data and secrets",
}
CHECK_LABELS = {
    "canary_leak": "Secret marker leaked",
    "prompt_fragment_leak": "Instruction text leaked",
    "forbidden_text": "Forbidden statement",
    "required_text": "Required information missing",
    "disallowed_codes": "Unapproved discount code",
    "fact_mismatch": "Wrong policy fact",
    "pii_leak": "Private data exposed",
    "ai_disclosure": "No AI disclosure",
    "judge": "Judge check",
}
REPLAY_LABEL = (
    "Recorded in a real browser during a replay. The bot's wording may differ slightly from the original test."
)
NEXT_STEP_FINDINGS = "Forward this report to your developer. Most fixes are changes to the bot's instructions."
NEXT_STEP_CLEAN = (
    "No issues found in this run. Retest after any change to your bot's model, instructions or knowledge base."
)


def highlight(text: str, terms: list[str]) -> Markup:
    """HTML-escape untrusted text, then wrap every occurrence of each term in <mark>."""
    terms = sorted({term for term in terms if term}, key=len, reverse=True)
    if not terms:
        return Markup(escape(text))
    pattern = "|".join(re.escape(term) for term in terms)
    parts: list[Markup] = []
    position = 0
    for match in re.finditer(pattern, text, re.IGNORECASE):
        parts.append(escape(text[position : match.start()]))
        parts.append(Markup("<mark>") + escape(match.group(0)) + Markup("</mark>"))
        position = match.end()
    parts.append(escape(text[position:]))
    return Markup("").join(parts)


def _transcript(exchange: Exchange | None, finding: Finding) -> list[dict[str, Any]]:
    if exchange is None:
        return []
    bubbles: list[dict[str, Any]] = []
    for turn in exchange.turns:
        bubbles.append({"role": "customer", "html": escape(turn.sent)})
        if turn.reply is not None:
            terms = [
                check.matched_text
                for check in finding.checks
                if check.matched_text and (check.turn_index is None or check.turn_index == turn.index)
            ]
            bubbles.append({"role": "bot", "html": highlight(turn.reply, terms)})
        elif turn.error:
            bubbles.append({"role": "error", "html": escape(f"No reply: {turn.error}")})
    return bubbles


def _passed_by_category(
    exchanges: list[Exchange], verdicts: list[Verdict], attacks: dict[str, Attack]
) -> list[dict[str, Any]]:
    failed = {verdict.attack_id for verdict in verdicts if verdict.verdict == "fail"}
    rows = []
    for category, name in CATEGORY_NAMES.items():
        in_category = [e for e in exchanges if e.attack_id in attacks and attacks[e.attack_id].category == category]
        if not in_category:
            continue
        passed = sum(1 for e in in_category if e.status == "completed" and e.attack_id not in failed)
        rows.append({"name": name, "passed": passed, "total": len(in_category)})
    return rows


def _existing(run_dir: Path, relative: str | None) -> str | None:
    """The relative evidence path, if that file really exists in the run folder."""
    return relative if relative and (run_dir / relative).is_file() else None


def _evidence(run_dir: Path, original: Exchange | None, replay: Exchange | None) -> dict[str, Any]:
    """Screenshot/video for a finding card: the original run's evidence first, otherwise the replay's."""
    for exchange, is_replay in ((original, False), (replay, True)):
        if exchange is None:
            continue
        screenshot = _existing(run_dir, exchange.evidence_screenshot)
        video = _existing(run_dir, exchange.evidence_video)
        if screenshot or video:
            return {"screenshot": screenshot, "video": video, "replay_label": REPLAY_LABEL if is_replay else None}
    return {"screenshot": None, "video": None, "replay_label": None}


def render_report_html(run_dir: Path, pdf_link: bool) -> tuple[str, Grade]:
    """Render report.html for a run folder and return (html, grade)."""
    info = RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE))
    exchanges = read_latest_exchanges(run_dir)
    verdicts = read_jsonl(run_dir / VERDICTS_FILE, Verdict)
    findings = [Finding.model_validate(item) for item in read_json(run_dir / FINDINGS_FILE)]
    attacks = {attack.id: attack for attack in load_attacks(Path(info.attack_pack_dir))}
    exchanges_by_id = {exchange.attack_id: exchange for exchange in exchanges}
    replays_by_id = {
        replay.attack_id: replay for replay in latest_exchanges(read_jsonl(run_dir / REPLAYS_FILE, Exchange))
    }
    grade = calculate_grade(findings, exchanges, verdicts)

    cards = []
    for number, finding in enumerate(findings, start=1):
        attack = attacks.get(finding.attack_id)
        cards.append(
            {
                "anchor": f"finding-{number}",
                "number": number,
                "severity": finding.severity,
                "title": finding.title,
                "owasp": attack.owasp if attack else "",
                "checks": [
                    {
                        "severity": check.severity,
                        "label": CHECK_LABELS.get(check.check_type, check.check_type),
                        "detail": check.detail,
                    }
                    for check in finding.checks
                ],
                "why": attack.why_it_matters if attack else "",
                "fix": attack.how_to_fix if attack else "",
                "copy_text": "\n".join(attack.turns) if attack else "",
                "transcript": _transcript(exchanges_by_id.get(finding.attack_id), finding),
                "evidence": _evidence(
                    run_dir, exchanges_by_id.get(finding.attack_id), replays_by_id.get(finding.attack_id)
                ),
            }
        )

    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("report.html.j2")
    html = template.render(
        info=info,
        run_date=info.started_at.astimezone().strftime("%d %B %Y"),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        pack_hash_short=info.attack_pack_hash[:12],
        grade=grade,
        findings=cards,
        top_findings=cards[:3],
        next_step=NEXT_STEP_FINDINGS if findings else NEXT_STEP_CLEAN,
        passed_by_category=_passed_by_category(exchanges, verdicts, attacks),
        pdf_link=pdf_link,
        pdf_file=REPORT_PDF_FILE,
        retest_href=f"mailto:{RETEST_EMAIL}?subject={quote(f'Retest request - run {info.run_id}')}",
    )
    return html, grade


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _print_pdf(html_path: Path, pdf_path: Path) -> str | None:
    """Print the HTML to an A4 PDF. Returns an error message, or None on success."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri())
            page.emulate_media(media="print")
            page.pdf(path=str(pdf_path), format="A4", print_background=True)
            browser.close()
    except PlaywrightError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return f"{first_line}\nInstall the browser with: playwright install chromium"
    return None


def build_report(run_dir: Path, pdf: bool = False, echo: Callable[[str], None] = print) -> Path:
    """Write report.html (and report.pdf if requested and possible). Returns the HTML path."""
    run_dir = require_run_dir(run_dir)
    html_path = run_dir / REPORT_HTML_FILE
    pdf_path = run_dir / REPORT_PDF_FILE

    want_pdf = pdf and _playwright_available()
    if pdf and not want_pdf:
        echo('PDF skipped: Playwright is not installed. Install it with: pip install -e ".[pdf]" '
             "then: playwright install chromium")

    html, _ = render_report_html(run_dir, pdf_link=want_pdf)
    html_path.write_text(html, encoding="utf-8")

    if want_pdf:
        error = _print_pdf(html_path, pdf_path)
        if error:
            echo(f"PDF skipped: {error}")
            html, _ = render_report_html(run_dir, pdf_link=False)
            html_path.write_text(html, encoding="utf-8")
        else:
            echo(f"PDF report: {pdf_path}")
    echo(f"HTML report: {html_path}")
    return html_path
