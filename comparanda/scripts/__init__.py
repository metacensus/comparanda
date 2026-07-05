"""Comparanda script package.

The organized import paths are:

- `scripts.extraction.*`
- `scripts.normalization.*`
- `scripts.review.*`

Compatibility aliases below keep previously generated extractor staging scripts
working when they import `scripts.extraction_sdk` or related extraction modules
directly. Do not add new top-level scripts here; new code should use the grouped
paths.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


_SCRIPTS_DIR = Path(__file__).resolve().parent
_EXTRACTION_DIR = _SCRIPTS_DIR / "extraction"
if str(_EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTRACTION_DIR))

_ALIASES = {
    "extraction_builder": "extraction.extraction_builder",
    "extraction_core": "extraction.extraction_core",
    "extraction_sdk": "extraction.extraction_sdk",
    "extraction_validator": "extraction.extraction_validator",
    "extractor_session_registry": "extraction.extractor_session_registry",
    "run_extraction_build": "extraction.run_extraction_build",
    "write_extraction": "extraction.write_extraction",
}

for old_name, new_name in _ALIASES.items():
    sys.modules.setdefault(
        f"{__name__}.{old_name}",
        import_module(f"{__name__}.{new_name}"),
    )
