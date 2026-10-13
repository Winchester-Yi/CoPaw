# -*- coding: utf-8 -*-
"""Make the independent Monitor package available to subsystem tests."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
MONITOR_SOURCE = str(ROOT / "monitor" / "src")
if MONITOR_SOURCE not in sys.path:
    sys.path.insert(0, MONITOR_SOURCE)
