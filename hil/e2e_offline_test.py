#!/usr/bin/env python3
"""Credential-free five-turn acoustic/VAD/gate acceptance."""
import argparse
from pathlib import Path
from e2e_common import run
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--port',default=None);p.add_argument('--runs',type=int,default=5)
    p.add_argument('--spl',type=float,choices=(65,58),default=65,help='Calibrated prompt SPL at the native board mic')
    p.add_argument('--no-retakes',action='store_true',help='Retain failed originals without repeat acquisitions')
    p.add_argument('--diagnostic-noisy',action='store_true',help='Continue measurements in noise, but never count a contaminated turn as passed')
    p.add_argument('--warm',action='store_true',help='Keep one session after the first cold turn')
    p.add_argument('--elf',type=Path,help='Verify a saved firmware image')
    p.add_argument('--server-url',help='RAM-only board signaling endpoint')
    p.add_argument('--ice-servers',nargs='+',default=[])
    p.add_argument('--option',action='append',default=[])
    p.add_argument('--display',choices=('cat',),help='Use production display fixture/bench before acoustic windows; requires owned asset origin')
    raise SystemExit(run(p.parse_args(),False))
