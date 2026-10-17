"""`audit package`: zip a run's report and the evidence it references, for delivery to the customer."""

from __future__ import annotations

import html
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

from chatbot_audit.config_loader import ConfigError
from chatbot_audit.data_models import RunInfo
from chatbot_audit.pipeline.run_folder import (
    EVIDENCE_DIR,
    REPORT_HTML_FILE,
    REPORT_PDF_FILE,
    RUN_INFO_FILE,
    read_json,
    require_run_dir,
)

EVIDENCE_LINK_RE = re.compile(r'(?:src|href)="(' + EVIDENCE_DIR + r'/[^"]+)"')


def referenced_evidence(report_html: str) -> list[str]:
    """Evidence paths (relative to the run folder) that the report links to, in order."""
    return list(dict.fromkeys(html.unescape(path) for path in EVIDENCE_LINK_RE.findall(report_html)))


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def package_run(run_dir: Path, echo: Callable[[str], None] = print) -> Path:
    """Write <run>/<client>-<run_id>-report.zip with report.html, report.pdf and referenced evidence."""
    run_dir = require_run_dir(run_dir)
    report_path = run_dir / REPORT_HTML_FILE
    if not report_path.is_file():
        raise ConfigError(f"No {REPORT_HTML_FILE} in {run_dir}. Run 'audit report --run {run_dir}' first.")
    info = RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE))
    zip_path = run_dir / f"{info.client}-{info.run_id}-report.zip"

    evidence_root = (run_dir / EVIDENCE_DIR).resolve()
    members = [REPORT_HTML_FILE]
    if (run_dir / REPORT_PDF_FILE).is_file():
        members.append(REPORT_PDF_FILE)
    for relative in referenced_evidence(report_path.read_text(encoding="utf-8")):
        source = (run_dir / relative).resolve()
        if source.is_file() and source.is_relative_to(evidence_root):
            members.append(relative)
        else:
            echo(f"Warning: the report links to {relative}, which is missing; it is not in the zip.")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.write(run_dir / member, arcname=member)
    echo(f"Package: {zip_path} ({_human_size(zip_path.stat().st_size)}, {len(members)} files)")
    return zip_path
