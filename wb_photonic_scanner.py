#!/usr/bin/env python3
"""
wb_photonic_scanner.py — Photonic CPO ETF scanner wrapper for Wanna Buffet.
Executes anchor_scanner.py targeting the Photonic CPO ETF wallet.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The repo's own interpreter, not whatever launched this file: the Hermes
# scheduler has been running scripts with a Python that lacks `requests`
# (every run from 2026-09-29 to 2026-10-02 crashed with ModuleNotFoundError,
# which also woke the model each time for nothing).
PY = HERE / "venv" / "bin" / "python"
cmd = [str(PY if PY.exists() else sys.executable), str(HERE / "anchor_scanner.py"), "--wallet", "Photonic CPO ETF"]
if len(sys.argv) > 1:
    cmd.extend(sys.argv[1:])

result = subprocess.run(cmd, cwd=str(HERE))
sys.exit(result.returncode)
