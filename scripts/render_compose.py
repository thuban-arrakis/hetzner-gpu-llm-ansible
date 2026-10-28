#!/usr/bin/env python3
"""Render the default Compose template for offline schema validation."""

import json
from pathlib import Path
import sys

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import yaml


def render(root: Path, accelerator: str = "nvidia") -> str:
    if accelerator not in ("nvidia", "cpu"):
        raise ValueError("Choose nvidia or explicit cpu mode")
    role = root / "roles" / "ollama"
    defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text())
    defaults["inference_accelerator"] = accelerator
    environment = Environment(
        loader=FileSystemLoader(role / "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["to_json"] = json.dumps
    return environment.get_template("compose.yml.j2").render(defaults)


if __name__ == "__main__":
    sys.stdout.write(render(Path(__file__).resolve().parents[1]))
