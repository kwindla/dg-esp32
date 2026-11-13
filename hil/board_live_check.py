#!/usr/bin/env python3
"""Coordinator-only board identity/boot witness; requires runner-owned bench FD."""
import argparse
import json
from pathlib import Path
from boards import hardware_board, require_bench, verify_flash_mac
from board_serial import open_board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', default='p4xc')
    parser.add_argument('--boot-pattern', default=None)
    parser.add_argument('--baud', type=int, help='115200 for current spikes; default is product app baud')
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args(); info = hardware_board(args.board); require_bench()
    port = verify_flash_mac(board=args.board)
    kw = dict(port=port, log_path=args.log)
    if args.board == 'p4xc': kw['boot_pattern'] = args.boot_pattern
    if args.baud is not None: kw['baud'] = args.baud
    with open_board(args.board, **kw) as board:
        line = board.boot_line if args.board == 'p4xc' else board.wait_for(args.boot_pattern or info['boot_pattern'], 30, since=0)
        print(json.dumps(dict(board=args.board, port=port, efuse_mac=info['efuse_mac'],
                             baud=board.s.baudrate, reset_on_open=info['reset_on_open'], boot_line=line)))


if __name__ == '__main__': main()
