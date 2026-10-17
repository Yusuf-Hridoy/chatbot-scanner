"""Create and locate run folders; read and write the JSON/JSONL files inside them."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from chatbot_audit.config_loader import ConfigError
from chatbot_audit.data_models import ClientConfig

RUN_INFO_FILE = "run_info.json"
EXCHANGES_FILE = "exchanges.jsonl"
VERDICTS_FILE = "verdicts.jsonl"
FINDINGS_FILE = "findings.json"
REPORT_HTML_FILE = "report.html"
REPORT_PDF_FILE = "report.pdf"

ModelT = TypeVar("ModelT", bound=BaseModel)


def runs_dir(config_path: Path) -> Path:
    """The folder that holds all runs for a client config."""
    return Path(config_path).resolve().parent / "runs"


def create_run(config_path: Path, config: ClientConfig, tier: str | None = None) -> Path:
    """Create `<config dir>/runs/<YYYYMMDD-HHMMSS-tier>/` and return its path."""
    base_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tier or config.tier}"
    parent = runs_dir(config_path)
    parent.mkdir(parents=True, exist_ok=True)
    run_dir = parent / base_id
    suffix = 2
    while run_dir.exists():  # two runs started in the same second
        run_dir = parent / f"{base_id}-{suffix}"
        suffix += 1
    run_dir.mkdir()
    return run_dir


def locate_run(config_path: Path, run_id: str) -> Path:
    """Find an existing run folder by id, or fail clearly."""
    run_dir = runs_dir(config_path) / run_id
    if not run_dir.is_dir():
        raise ConfigError(f"Run '{run_id}' not found in {runs_dir(config_path)}")
    return run_dir


def require_run_dir(run_dir: Path) -> Path:
    """Check that a run folder exists and has run_info.json."""
    run_dir = Path(run_dir)
    if not (run_dir / RUN_INFO_FILE).is_file():
        raise ConfigError(f"{run_dir} is not a run folder (no {RUN_INFO_FILE} found).")
    return run_dir


def append_jsonl(path: Path, model: BaseModel) -> None:
    """Append one model as a JSON line and flush it to disk immediately."""
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(model.model_dump_json() + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path, model_cls: type[ModelT]) -> list[ModelT]:
    """Read every line of a JSONL file. An incomplete last line is ignored with a warning."""
    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    items: list[ModelT] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        is_last = number == len(lines) or all(not rest.strip() for rest in lines[number:])
        try:
            items.append(model_cls.model_validate_json(line))
        except ValidationError as exc:
            if is_last:
                print(f"Warning: ignoring incomplete last line {number} in {path.name}", file=sys.stderr)
                continue
            raise ConfigError(f"{path.name} line {number} is corrupted: {exc.errors()[0]['msg']}") from exc
    return items


def repair_jsonl(path: Path) -> None:
    """Drop an incomplete trailing line (left by a crash) so new lines can be appended safely."""
    path = Path(path)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        path.write_text(text[: text.rfind("\n") + 1], encoding="utf-8")


def write_json(path: Path, data: BaseModel | list[BaseModel] | dict[str, Any]) -> None:
    """Write a model, list of models or plain dict as pretty JSON."""
    if isinstance(data, BaseModel):
        payload: Any = data.model_dump(mode="json")
    elif isinstance(data, list):
        payload = [item.model_dump(mode="json") for item in data]
    else:
        payload = data
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    """Read a JSON file, failing clearly if it is missing."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Missing file {path.name} in {path.parent}. Run the earlier pipeline step first.")
    return json.loads(path.read_text(encoding="utf-8"))
