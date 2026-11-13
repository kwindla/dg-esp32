#!/usr/bin/env python3
"""Step 4: root firmware /start check; run the offline server on port 7861 first."""
import argparse
from pathlib import Path
import re
import time
from esp_serial import EspSerial, ROOT


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', default='/dev/ttyACM0')
    p.add_argument('--out', type=Path, default=ROOT / 'proj-2026-10-08-0949/evidence/step4')
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with EspSerial(a.port, log_path=a.out / 'serial.log') as serial:
        cursor = serial.cursor
        serial.reset()
        line = serial.wait_for(r'on_bot_started sessionId=[0-9a-f-]{36}', 40, since=cursor)
        serial.wait_for(r'START_BOT PASS', 10, since=cursor)
        time.sleep(2)  # Include teardown and catch a reboot after the success line.
        entries = serial.lines_since(cursor)
        assert not any(re.search(r'Guru Meditation|START_BOT FAIL|abort\(\)|assert failed', x[2]) for x in entries)
        heaps = [x[2] for x in entries if 'HEAP internal=' in x[2]]
        stacks = [x[2] for x in entries if 'STACK task=' in x[2]]
        assert heaps and stacks, 'Resource evidence missing'
        for h in heaps:
            assert int(re.search(r'internal=(\d+)', h)[1]) >= 120 * 1024
            assert int(re.search(r'largest=(\d+)', h)[1]) >= 32 * 1024
        core_stacks = [x for x in stacks if 'task=pipecat ' in x or 'task=start_bot ' in x]
        assert len(core_stacks) >= 4, 'Missing core thread stack measurements'
        for s in core_stacks:
            assert int(re.search(r'free_bytes=(\d+)', s)[1]) >= 12288 // 4
        print(line)
        print('PASS: sessionId callback, internal heap/block floors, core stack margins >=25%')


if __name__ == '__main__':
    main()
