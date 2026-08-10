#!/usr/bin/env python3
"""Resolve a Comparanda domain project independently of the engine checkout.

The canonical engine can be embedded as a Git submodule while the application
state lives at the root of a single-domain superproject. Paths may be supplied
explicitly, through environment variables, or through
``comparanda-project.json`` in the project root.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class LayoutError(ValueError):
    """Raised when a domain-project layout cannot be resolved."""


def _path_from(root: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _nested(config: dict, *keys: str, default=None):
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


@dataclass(frozen=True)
class ProjectLayout:
    root: Path
    domain: str | None
    config_path: Path | None
    iterations_dir: Path
    eval_config_dir: Path
    gold_standard_dir: Path
    export_runs_dir: Path | None

    @classmethod
    def load(
        cls,
        project: str | os.PathLike[str] | None = None,
        config_path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ProjectLayout":
        env = os.environ if environ is None else environ

        explicit_config = Path(config_path).expanduser().resolve() if config_path else None
        project_value = project or env.get("COMPARANDA_PROJECT_DIR")
        if project_value:
            initial_root = Path(project_value).expanduser().resolve()
        elif explicit_config:
            initial_root = explicit_config.parent
        else:
            raise LayoutError(
                "missing project root: pass --project, set COMPARANDA_PROJECT_DIR, "
                "or pass --project-config"
            )

        discovered_config = explicit_config
        if discovered_config is None:
            candidate = initial_root / "comparanda-project.json"
            if candidate.is_file():
                discovered_config = candidate

        config: dict = {}
        if discovered_config:
            try:
                config = json.loads(discovered_config.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise LayoutError(
                    f"could not read project config {discovered_config}: {exc}"
                ) from exc
            if not isinstance(config, dict):
                raise LayoutError(f"project config must be a JSON object: {discovered_config}")

        configured_root = config.get("project_root")
        if configured_root is not None:
            base = discovered_config.parent if discovered_config else initial_root
            root = _path_from(base, configured_root)
        else:
            root = initial_root

        iterations_value = env.get("COMPARANDA_ITERATIONS_DIR") or config.get(
            "iterations", "iterations"
        )
        eval_config_value = env.get("COMPARANDA_EVAL_CONFIG_DIR") or _nested(
            config, "benchmark", "eval_config", default="benchmark/eval_config"
        )
        gold_standard_value = env.get("COMPARANDA_GOLD_STANDARD_DIR") or _nested(
            config, "benchmark", "gold_standard", default="benchmark/gold_standard"
        )
        export_runs_value = env.get("COMPARANDA_EXPORT_RUNS_DIR") or _nested(
            config, "export", "runs"
        )

        domain = config.get("domain")
        if domain is not None and not isinstance(domain, str):
            raise LayoutError("project config 'domain' must be a string")

        return cls(
            root=root,
            domain=domain,
            config_path=discovered_config,
            iterations_dir=_path_from(root, iterations_value),
            eval_config_dir=_path_from(root, eval_config_value),
            gold_standard_dir=_path_from(root, gold_standard_value),
            export_runs_dir=(
                _path_from(root, export_runs_value) if export_runs_value else None
            ),
        )

    def export_dir(
        self,
        run_id: str,
        explicit_out: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Path:
        env = os.environ if environ is None else environ
        out = explicit_out or env.get("COMPARANDA_EXPORT_DIR")
        if out:
            return _path_from(self.root, out)
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise LayoutError(
                "run ID must be a single directory name when deriving an export path"
            )
        if self.export_runs_dir is None:
            raise LayoutError(
                "missing export destination: pass --out, set COMPARANDA_EXPORT_DIR, "
                "or configure export.runs"
            )
        return self.export_runs_dir / run_id
