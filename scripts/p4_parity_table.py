#!/usr/bin/env python3
"""CLI for the step-16 evidence-backed Markdown generator; no device access."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'hil'))
from p4_step16_paths import table_main as main

if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
