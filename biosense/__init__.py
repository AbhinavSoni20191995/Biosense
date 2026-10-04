"""BioSense-AI deterministic simulation and outcome tools.

The LLM agents choose and explain tool calls. Every trajectory, metric,
comparison and next-test decision in this package is computed numerically.
"""
import sys as _sys
from pathlib import Path as _Path

# The literature tools (agent_tools.py) live at the repo root; keep one unit table.
_ROOT = str(_Path(__file__).resolve().parent.parent)
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
