"""Test-run environment defaults.

The arbitrage tests construct their own ArbSettings; these defaults keep
the process deterministic and prevent a test from starting the dashboard.
"""
from __future__ import annotations

import os

os.environ.setdefault("ARB_PAPER_TRADING", "true")
os.environ.setdefault("ARB_WEB_ENABLED", "false")
