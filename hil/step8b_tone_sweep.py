#!/usr/bin/env python3
"""Step-7 two-chain tone qualification with the candidate ceilings included."""
import argparse
from pathlib import Path
import speaker_dual_sweep_test as sweep
p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--port',default='/dev/ttyACM0');a=p.parse_args()
sweep.AMPLITUDES=(10,15,20,26,30)
raise SystemExit(sweep.run(a))
