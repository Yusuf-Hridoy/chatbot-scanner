"""Command-line interface: `audit <command>`."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Optional

import typer

from chatbot_audit.adapters.base_adapter import AdapterError, create_adapter
from chatbot_audit.adapters.widget_preset_loader import is_verified, list_presets, load_preset
from chatbot_audit.attacks.attack_loader import filter_by_tier, load_attacks
from chatbot_audit.config_loader import ConfigError, config_summary, load_client_config
from chatbot_audit.data_models import BrowserTargetConfig, Finding, Verdict
from chatbot_audit.pipeline.grade_calculator import calculate_grade
from chatbot_audit.pipeline.package_run import package_run
from chatbot_audit.pipeline.replay_findings import replay_findings
from chatbot_audit.pipeline.run_folder import VERDICTS_FILE, read_jsonl, read_latest_exchanges
from chatbot_audit.pipeline.step1_run_attacks import run_attacks, warn_if_unverified
from chatbot_audit.pipeline.step2_evaluate_results import evaluate_run
from chatbot_audit.pipeline.step4_build_report import build_report

app = typer.Typer(
    help="Audit customer-facing AI chatbots with scripted attacks and rule-based checks.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[Path, typer.Option("--config", help="Path to client_config.yaml.")]
RunOption = Annotated[Path, typer.Option("--run", help="Path to a run folder.")]
TierOption = Annotated[
    Optional[str], typer.Option("--tier", help="Override the config tier: quick, standard or deep.")
]
PdfOption = Annotated[bool, typer.Option("--pdf", help="Also print the report to PDF (needs Playwright).")]
HeadedOption = Annotated[bool, typer.Option("--headed", help="Show the browser window (browser targets only).")]
CHECK_SCREENSHOT = "check_screenshot"


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _check_tier(tier: str | None) -> None:
    if tier is not None and tier not in ("quick", "standard", "deep"):
        raise _fail(f"Unknown tier '{tier}'. Use quick, standard or deep.")


def _print_grade(run_dir: Path, findings: list[Finding]) -> None:
    exchanges = read_latest_exchanges(run_dir)
    verdicts = read_jsonl(run_dir / VERDICTS_FILE, Verdict)
    grade = calculate_grade(findings, exchanges, verdicts)
    typer.echo("")
    if grade.letter:
        typer.secho(f"Grade: {grade.letter} ({grade.score}/100)", bold=True)
    else:
        typer.secho(f"Grade: Incomplete ({grade.score}/100)", bold=True)
    if grade.note:
        typer.echo(grade.note)
    counts = grade.counts_by_severity
    typer.echo(
        f"Findings: {counts['critical']} critical, {counts['high']} high, "
        f"{counts['medium']} medium, {counts['low']} low"
    )
    typer.echo(f"Tests passed: {grade.passed_tests}/{grade.total_tests} (inconclusive: {grade.inconclusive_count})")


@app.command()
def validate(config: ConfigOption) -> None:
    """Validate the client config and all attack packs."""
    try:
        client_config = load_client_config(config)
        attacks = load_attacks()
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    selected = filter_by_tier(attacks, client_config.tier)
    typer.secho("Config and attack packs are valid.", fg=typer.colors.GREEN)
    typer.echo(json.dumps(config_summary(client_config), indent=2, ensure_ascii=False))
    typer.echo(f"\nAttacks selected for tier '{client_config.tier}': {len(selected)} of {len(attacks)}")
    for category, count in sorted(Counter(attack.category for attack in selected).items()):
        typer.echo(f"  {category:<16}{count:>3}")


@app.command()
def check(config: ConfigOption) -> None:
    """Pre-flight: send "Hello" to the bot and show the reply."""
    try:
        client_config = load_client_config(config)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc

    is_browser = isinstance(client_config.target, BrowserTargetConfig)
    if is_browser:
        client_config.target.record_video = False  # a screenshot is enough for a pre-flight check
        warn_if_unverified(client_config.target, typer.echo)
    screenshot_dir = Path(config).resolve().parent if is_browser else None

    async def ping() -> tuple[str, int, Path | None]:
        adapter = create_adapter(client_config)
        try:
            conversation_id = await adapter.start_conversation()
            try:
                reply = await adapter.send(conversation_id, "Hello")
            finally:
                evidence = await adapter.end_conversation(conversation_id, screenshot_dir, CHECK_SCREENSHOT)
            return reply.text, reply.latency_ms, evidence.screenshot_path
        finally:
            await adapter.close()

    try:
        text, latency, screenshot = asyncio.run(ping())
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    except AdapterError as exc:
        raise _fail(f"The chatbot is not reachable. {exc}", code=2) from exc
    typer.secho(f"Connected to {client_config.display_name} ({client_config.target.type}).", fg=typer.colors.GREEN)
    typer.echo(f"Sent:    Hello\nReply:   {text}\nLatency: {latency} ms")
    if screenshot:
        typer.echo(f"Screenshot: {screenshot}")


@app.command()
def run(
    config: ConfigOption,
    tier: TierOption = None,
    resume: Annotated[Optional[str], typer.Option("--resume", help="Run id to resume.")] = None,
    headed: HeadedOption = False,
) -> None:
    """Step 1: send the attacks and record the conversations."""
    _check_tier(tier)
    try:
        run_dir = run_attacks(config, tier=tier, resume_run_id=resume, echo=typer.echo, headed=headed)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"\nRun complete. Run folder: {run_dir}")


@app.command()
def evaluate(run: RunOption) -> None:
    """Step 2: check every reply against the rules and write findings."""
    try:
        evaluate_run(run, echo=typer.echo)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def report(run: RunOption, pdf: PdfOption = False) -> None:
    """Step 4: build report.html (and report.pdf with --pdf)."""
    try:
        build_report(run, pdf=pdf, echo=typer.echo)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def full(config: ConfigOption, tier: TierOption = None, pdf: PdfOption = False, headed: HeadedOption = False) -> None:
    """Run steps 1, 2 and 4 in sequence and print the grade."""
    _check_tier(tier)
    try:
        run_dir = run_attacks(config, tier=tier, echo=typer.echo, headed=headed)
        findings = evaluate_run(run_dir, echo=typer.echo)
        typer.echo("")
        html_path = build_report(run_dir, pdf=pdf, echo=typer.echo)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    _print_grade(run_dir, findings)
    typer.secho(f"Report: {html_path}", fg=typer.colors.GREEN)


@app.command()
def connect(
    url: Annotated[str, typer.Option("--url", help="Page with the chat widget.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the target block.")] = Path(
        "target_recorded.yaml"
    ),
    frame_auto: Annotated[
        bool, typer.Option("--frame-auto/--no-frame-auto", help="Detect the chat iframe automatically.")
    ] = True,
) -> None:
    """Record chat widget selectors by clicking on the page (opens a browser window)."""
    from chatbot_audit.connect.selector_recorder import run_connect

    try:
        asyncio.run(run_connect(url, out, frame_auto=frame_auto))
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt as exc:
        raise _fail("Cancelled.") from exc


@app.command()
def replay(run: RunOption, headed: HeadedOption = False) -> None:
    """Re-run attacks with findings in a real browser (evidence_target) to record video evidence."""
    try:
        replay_findings(run, echo=typer.echo, headed=headed)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    except AdapterError as exc:
        raise _fail(str(exc), code=2) from exc
    typer.echo(f"Rebuild the report to include the replays: audit report --run {run}")


@app.command()
def package(run: RunOption) -> None:
    """Zip the report and its evidence for delivery to the customer."""
    try:
        package_run(run, echo=typer.echo)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command()
def presets() -> None:
    """List widget presets for browser targets."""
    for name in list_presets():
        data = load_preset(name)
        flag = "verified" if is_verified(name) else "UNVERIFIED"
        typer.echo(f"{name:<10} {flag:<11} {' '.join(str(data.get('description', '')).split())}")


@app.command("demo-bot")
def demo_bot(
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
    safe: Annotated[bool, typer.Option("--safe", help="Run the fixed (safe) version.")] = False,
) -> None:
    """Start the Lumen Skincare demo chatbot."""
    from chatbot_audit.demo_bot.vulnerable_demo_bot import serve

    try:
        serve(port=port, safe_mode=safe)
    except OSError as exc:
        raise _fail(f"Could not start the demo bot on port {port}: {exc}") from exc


if __name__ == "__main__":
    app()
