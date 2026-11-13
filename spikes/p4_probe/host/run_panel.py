#!/usr/bin/env python3
"""Coordinator-only panel timing sweep; visual stability needs an explicit witness."""
import argparse
import json
from pathlib import Path
from console_rate import Board
from hardware import require_bench
import measure_b as analysis


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--clocks',type=int,nargs='+',default=[80,60,50,45,40,38,36])
    parser.add_argument('--seconds',type=int,default=10)
    parser.add_argument('--visual-stable-clocks',type=int,nargs='*',default=[],
                        help='only clocks the user/coordinator actually observed stable; omitted means timing only')
    args=parser.parse_args()
    if not all(30<=clock<=80 for clock in args.clocks) or not 2<=args.seconds<=30: parser.error('Clocks 30..80 MHz, seconds 2..30')
    if not set(args.visual_stable_clocks)<=set(args.clocks): parser.error('Visual witness must refer to tested clocks')
    args.out.mkdir(parents=True,exist_ok=True);result=dict(runs=[])
    try:
        require_bench()
        with (args.out/'serial.log').open('wb') as log, Board(log=log) as board:
            board.reset();board.until(lambda row:row.startswith(b'P4_PANEL READY'),30);board.switch_baud(2000000)
            for clock in args.clocks:
                raw=board.command(f'panel {clock} {args.seconds}',args.seconds+45)
                rows=analysis.records(raw)
                run=analysis.panel(rows)
                run['driver_underruns_observed']=sum(b'underrun' in line.lower() for line in raw)
                if run['config']['clock_mhz']!=clock: raise ValueError('Panel response clock mismatch')
                if clock in args.visual_stable_clocks: run['visual_stability']='confirmed by coordinator/user'
                result['runs'].append(run)
        result.update(analysis.select_panel(result['runs'],args.visual_stable_clocks))
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}';raise
    finally:
        (args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__': main()
