"""Test path setup for the copilot package."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # copilot
sys.path.insert(0, str(_HERE.parent / "mcp-server"))  # day2_mcp
sys.path.insert(0, str(_HERE.parents[1] / "core"))  # day2_agents
