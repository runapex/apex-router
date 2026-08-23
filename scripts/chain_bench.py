#!/usr/bin/env python3
"""Thin shim -> apex_router.chain_bench (the importable module)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from apex_router.chain_bench import main

if __name__ == "__main__":
    raise SystemExit(main())
