#!/usr/bin/env python3
"""Offline stable/development channel entry point for this source checkout."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_agent.release import main


if __name__ == "__main__":
    raise SystemExit(main())
