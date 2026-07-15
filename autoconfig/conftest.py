"""Pytest / unittest discovery hook.

The canonical autoconfig_poc.py + benchmark.py live inside the skill bundle
at skill/llm-d-autoconfig/scripts/ — that's the install location for the
agent skill, and we keep ONE copy (no root duplicate that has to be synced).

Tests import them with `from autoconfig_poc import ...`, which only works
if the skill scripts directory is on sys.path. This module runs at test
discovery time and inserts that directory.

Also picked up by plain `python3 -m unittest discover -s tests` because
unittest doesn't run conftest.py — so we additionally have to make sure
the scripts directory is on sys.path BEFORE any test module imports.
The trick: this file is at the repo root, and `python3 -m unittest
discover -s tests` runs with the repo root on sys.path (cwd=repo root by
default), so importing this module from tests/__init__.py or via early
sys.path manipulation in tests works.
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent / "skill" / "llm-d-autoconfig" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
