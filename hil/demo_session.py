#!/usr/bin/env python3
"""Owned EchoEar voice+cat demo. No hardware is accessed with --check-only."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path('/home/khkramer/src/evidence/echoear/B')
# Include the binary fixtures embedded by main/CMakeLists.txt. Server-only or
# harness-only master commits need not change the frozen firmware image.
FIRMWARE_PATHS = ['CMakeLists.txt', 'cmake', 'main', 'components', 'third_party',
                  'scripts', 'partitions.csv', 'sdkconfig.defaults*', 'dependencies.lock',
                  'server/fixtures', 'hil/fixtures/lean_resume.opus']


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def firmware_artifacts(build, expected_version):
    """Reject stale ELF/bin after a failed incremental build refreshes metadata."""
    elf=(build/'dg-esp32.elf').read_bytes()
    image=(build/'dg-esp32.bin').read_bytes()
    descriptor=image[32:288] # ESP image header + first segment header
    if len(descriptor)!=256 or descriptor[:4]!=bytes.fromhex('3254cdab'):
        raise RuntimeError('Missing ESP application descriptor')
    version=descriptor[16:48].split(b'\0',1)[0].decode('ascii')
    if version!=expected_version or elf.count(descriptor[:144])!=1:
        raise RuntimeError('Stale firmware artifacts: embedded ELF/bin version differs from build metadata')
    digest=hashlib.sha256(elf).digest()
    if descriptor[144:176]!=digest:
        raise RuntimeError('Application binary does not match local ELF SHA256')
    return digest.hex()


def preflight(firmware_ref="main"):
    from step9a_echoear import require_echoear_image, check_esptool
    check_esptool()
    require_echoear_image()
    description = json.loads((ROOT/'build/project_description.json').read_text())
    elf_hash=firmware_artifacts(ROOT/'build',description['project_version'])
    built_commit = git('rev-parse', description['project_version']+'^{commit}')
    master = git('rev-parse', 'main')
    expected = git('rev-parse', firmware_ref+'^{commit}')
    for args in ((built_commit, expected), (expected,)):
        if git('diff', '--name-only', *args, '--', *FIRMWARE_PATHS):
            raise RuntimeError('Local ELF/source differs from requested firmware ref; build that ref before the demo')
    for path in ('server/demo_bot.py', 'server/.venv/bin/python',
                 'build/dg-esp32.bin', 'build/flash_args', 'hil/fixtures/echoear360/codec-benchmark.json'):
        if not (ROOT/path).is_file():
            raise RuntimeError('Missing demo prerequisite: '+path)
    if git('diff', '--name-only', expected, '--', 'server/demo_bot.py'):
        raise RuntimeError('demo_bot.py differs from requested firmware ref')
    autostart='#define CONFIG_DG_DEMO_AUTOSTART 1' in (ROOT/'build/config/sdkconfig.h').read_text()
    return dict(demo_autostart=autostart,master_commit=master, requested_firmware_ref=firmware_ref,
                expected_firmware_commit=expected, checkout_commit=git('rev-parse', 'HEAD'),
                firmware_build_commit=built_commit,
                elf_sha256=elf_hash,
                firmware_basis='Built commit and working firmware inputs match requested ref; boot hash must match local ELF')


def nic_snapshot():
    counters = {}
    for line in Path('/proc/net/dev').read_text().splitlines():
        if ':' not in line:
            continue
        name, fields = line.split(':', 1)
        if name.strip() in ('enp7s0', 'wlp8s0'):
            values = fields.split()
            counters[name.strip()] = dict(rx_bytes=int(values[0]), tx_bytes=int(values[8]))
    return dict(monotonic_s=time.monotonic(), interfaces=counters)


def copy_bot_output(stream, out):
    with (out/'bot.log').open('w') as log, (out/'transcripts.log').open('w') as transcripts:
        for line in stream:
            log.write(line)
            log.flush()
            if 'Transcript: ' in line:
                transcripts.write(line)
                transcripts.flush()
                print(line.rstrip(), flush=True)


@contextmanager
def demo_server(out):
    # This context is entered only while the caller owns the bench lock.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 7861)) == 0:
            raise RuntimeError('Port 7861 is occupied; refusing to replace its owner')
    env = dict(os.environ, SERVER_PORT='7861', PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',
               PYTHONUNBUFFERED='1', LOGURU_COLORIZE='NO')
    proc = subprocess.Popen([str(ROOT/'server/.venv/bin/python'),
                             str(ROOT/'server/demo_bot.py'), '--host', '0.0.0.0', '--port', '7861'],
                            cwd=ROOT/'server', env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    reader = threading.Thread(target=copy_bot_output, args=(proc.stdout, out), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic()+60
        while True:
            if proc.poll() is not None:
                raise RuntimeError('Demo bot exited during startup; see bot.log')
            try:
                with urllib.request.urlopen('http://127.0.0.1:7861/', timeout=.5) as response:
                    if response.status < 500:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Demo bot startup; see bot.log')
            time.sleep(.1)
        yield proc
    finally:
        # Own process group only; never kill a pre-existing bot or touch 7860.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        reader.join(timeout=3)
        proc.stdout.close()


def prepare_board(board, url, budget_path, volume_db=0):
    import step9a_support as display
    board.command('quiet 1')
    board.command('audio 0')
    board.command('server-url '+url)
    board.command('playback-volume '+str(volume_db))
    cursor=board.cursor;board.command('stats')
    initial=json.loads(board.wait_for(r'^PERF \{',5,since=cursor).split('PERF ',1)[1])
    if initial.get('display',{}).get('lcd_initialized'):
        board.command('disp character off')
    else:
        board.command('disp init',45)
    status = display.read(board, 'status', 'DISPLAY')
    if status['flash']['health'] != 'ready':
        # Firmware refuses unless the whole storage region is already erased.
        # Never erase existing data or auto-format an unknown/corrupt partition.
        board.command('disp provision-empty', 90)
        status = display.read(board, 'status', 'DISPLAY')
        if status['flash']['health'] != 'ready':
            raise RuntimeError('Display storage is not ready after empty-only provisioning')
    cursor = board.cursor
    board.command('stats')
    stats = json.loads(board.wait_for(r'^PERF \{', 5, since=cursor).split('PERF ', 1)[1])
    heap = stats['heap']['psram']
    budget = dict(budget=491520, F=heap['free'], W=1287760, largest=heap['largest'])
    budget_path.write_text(json.dumps(budget, indent=2)+'\n')
    display.BUDGET_PATH = budget_path
    # Preserve Claude's duplicate-init fix with an explicit setup option.
    options=dict(initialized=True)
    if status.get('ram',{}).get('budget'):
        if status['ram']['budget']!=budget['budget']:raise RuntimeError('Existing arena differs from demo budget')
        options['arena_initialized']=True
    result = display.setup(board, **options)
    board.command('disp character on')
    return result


class DemoSerial:
    """Only serial is replaceable. Neither server belongs to this lifetime."""
    def __init__(self, port, out, events):
        from esp_serial import EspSerial
        self.factory = EspSerial
        self.out, self.events = out, events
        self.board = EspSerial(port, log_path=out/'serial.log')
        self.attempt = 0
        self.next_retry = 0
        self.sequence = 0

    def event(self, kind, **data):
        row = dict(kind=kind, monotonic_s=time.monotonic(), **data)
        self.events.append(row)
        with (self.out/'serial-lifecycle.jsonl').open('a') as stream:
            stream.write(json.dumps(row)+'\n')
        print('Demo serial: '+kind, flush=True)

    def detach(self, reason):
        if self.board is not None:
            self.sequence = getattr(self.board, '_seq', 0)
            try:
                self.board.close()
            except OSError as exc:
                reason += f'; close: {exc}'
            self.board = None
            self.event('detached', reason=reason)
        self.next_retry = time.monotonic()+2

    def poll(self, *, reconnect=True):
        from esp_serial import SerialDisconnected
        from board_port import resolve_port
        if self.board is not None:
            try:
                self.board.lines_since(self.board.cursor)
            except (SerialDisconnected, OSError) as exc:
                self.detach(str(exc))
            if self.board is not None:
                try:
                    resolve_port()  # sysfs/by-id only; never esptool/reset on return
                except (OSError, RuntimeError) as exc:
                    self.detach(str(exc))
        if self.board is None and reconnect and time.monotonic() >= self.next_retry:
            candidate = None
            try:
                port = resolve_port()  # missing or wrong MAC is never opened
                self.attempt += 1
                candidate = self.factory(port, log_path=self.out/f'serial-reattach-{self.attempt}.log',
                                         reset_on_open=False)
                candidate._seq = self.sequence
                # Retire any command fragment left at the point of removal.
                candidate.s.write(b'\n')
                candidate._snapshot_in_progress = True  # a missing reader is expected here
                candidate.command('ping', 2)
                candidate._snapshot_in_progress = False
                self.board = candidate
                self.event('reattached', attempt=self.attempt)
            except (OSError, RuntimeError, TimeoutError) as exc:
                if candidate is not None:
                    self.sequence = candidate._seq
                    candidate.close()
                self.next_retry = time.monotonic()+2
                self.event('reattach-wait', reason=str(exc))
        return self.board

    def close(self):
        if self.board is not None:
            self.board.close()
            self.board = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def cleanup_board(board):
    from esp_serial import SerialDisconnected
    errors = []
    if board is None:
        return errors
    for command, timeout in (('audio 0', 5), ('disconnect', 40), ('disp character off', 5), ('disp bench off', 5)):
        try:
            board.command(command, timeout)
        except (SerialDisconnected, OSError, TimeoutError) as exc:
            errors.append(f'{command}: serial unreachable; remaining cleanup skipped: {exc}')
            break
        except Exception as exc:
            errors.append(f'{command}: {exc}')
    return errors



def provision_assets(board, url, budget_path, volume_db=-6):
    import step9a_support as display
    from step9a_support import host_address
    board.command('demo hold', 45)
    board.command('demo configure '+json.dumps(dict(url=url,volume_db=volume_db,enabled=True),separators=(',',':')))
    setup=prepare_board(board,url,budget_path,volume_db=volume_db)
    persisted=[]
    for index,image in enumerate({x['hash']:x for x in setup['images']}.values()):
        rid='demo-persist-'+str(index)
        display.request(board,'persist',dict(hash=image['hash']),rid)
        result=display.terminal(board,rid)
        if result['error']!='none' or not result.get('persisted'):
            raise RuntimeError('Cat persistence not confirmed: '+image['hash'])
        persisted.append(image['hash'])
    status=[]
    if (ROOT/'hil/echoear_status_assets.py').is_file():
        from echoear_status_assets import provision_status, status_assets
        status=provision_status(board,'http://'+host_address()+':7874')
        persisted.extend(x['hash'] for x in status_assets())
    inventory=[];start=0
    while True:
        page=display.read(board,'inventory '+str(start),'DISPLAY_INVENTORY')
        inventory.extend(page['items'])
        start=page['next']
        if not start or start>=256:break
    known={x[0]:x for x in inventory}
    if any(h not in known or not known[h][4] or not known[h][5] or known[h][6]!='none' for h in persisted):
        raise RuntimeError('Persisted inventory verification failed')
    return dict(persisted_hashes=persisted,status_results=status,inventory=inventory,setup=setup,
                status_assets_available=bool(status),volume_db=volume_db)


def provision_session(out, *, flash_image=False):
    from step9a_echoear import bench,flash,verify,check_firmware
    from step9a_assets import asset_server
    from step9a_support import host_address
    from esp_serial import EspSerial
    result=dict(passed=False,cleanup_errors=[],purpose='One-time persisted standalone provisioning')
    try:
        with bench():
            if flash_image:flash(out)
            port=verify(out)
            with asset_server(), EspSerial(port,log_path=out/'provision-serial.log',display_checks=True) as board:
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                result['elf_sha256']=check_firmware(board,cursor)
                result['assets']=provision_assets(board,'http://'+host_address()+':7861',out/'arena-budget.json')
                cursor=board.cursor;board.command('power')
                result['power']=json.loads(board.wait_for(r'^POWER \{',5,since=cursor).split('POWER ',1)[1])
                result['provision_perf']=capture_demo_perf(board,out,'provision-perf')
                result['monitor']=board.display_monitor.result()
                if not result['power']['valid'] or not result['monitor']['passed'] or result['monitor']['errors']:
                    raise RuntimeError('Power readback or strict provisioning monitor failed')
                cursor=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=cursor)
                check_firmware(board,cursor)
                # No configuration/audio/connect commands after this final boot.
                result['passed']=True
                result['autonomous_boot_requested']=True
    except Exception as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        (out/'provision.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def capture_demo_perf(board, out, name):
    from transport_test import perf
    row = dict(monotonic_s=time.monotonic(), perf=perf(board))
    (out/(name+'.json')).write_text(json.dumps(row, indent=2)+'\n')
    return row


def unplug_counter_checks(before, after):
    from playback_quality import transport_gate, microphone_clip_gate
    transport = transport_gate(before, after)
    clips = microphone_clip_gate(before, after)
    same = bool(before.get('pc_id')) and before.get('pc_id') == after.get('pc_id') and before.get('generation') == after.get('generation')
    progress = all(after.get(k, 0)>before.get(k, 0) for k in ('sent', 'received'))
    return dict(passed=same and progress and transport['passed'] and clips['passed'],
                same_session=same, media_progress=progress, transport=transport, clips=clips,
                scope='Counter checks only; correlate physical absence and live turns with lifecycle/bot logs')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provision', action='store_true', help='Load, persist and verify cat/status assets and NVS settings, reboot, then observe autostart')
    parser.add_argument('--provision-only', action='store_true', help='Provision and exit after autonomous reboot; starts no bot')
    parser.add_argument('--observe', action='store_true', help='Start the bot and observe autostart with no host console commands after boot')
    parser.add_argument('--firmware-ref', default='main', help='Explicit approved firmware ref; source and ELF compatibility remain required')
    parser.add_argument('--unplug-check', action='store_true', help='Capture PERF before unplug and after each serial reattachment')
    parser.add_argument('--flash', action='store_true', help='Flash the verified local master-compatible image first')
    parser.add_argument('--check-only', action='store_true', help='Check local prerequisites only; no serial, servers or bench access')
    parser.add_argument('--out', type=Path, help='New log directory under the EchoEar evidence root')
    args = parser.parse_args(argv)
    if (args.observe or args.provision or args.provision_only) and args.unplug_check:parser.error('Observe mode sends no PERF commands')
    os.environ['DG_BOARD'] = 'echoear'
    os.environ['STEP9A_FIXTURES'] = str(ROOT/'hil/fixtures/echoear360')
    provenance = preflight(args.firmware_ref)
    if (args.provision or args.provision_only or args.observe) and not provenance.get('demo_autostart'):
        raise RuntimeError('Provision/observe requires a CONFIG_DG_DEMO_AUTOSTART demo image')
    if args.check_only:
        print(json.dumps(provenance, indent=2))
        return 0
    out = (args.out or EVIDENCE/('demo-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))).resolve()
    if not out.is_relative_to(EVIDENCE):
        parser.error('--out must be under '+str(EVIDENCE))
    out.mkdir(parents=True, exist_ok=False)
    if args.provision or args.provision_only:
        provisioned=provision_session(out,flash_image=args.flash)
        if not provisioned['passed']:return 1
        if args.provision_only:return 0
        args.flash=False;args.observe=True
    result = dict(provenance, ready=False, interrupted=False, cleanup_errors=[], serial_events=[],
                  purpose='Interactive demo, not qualification', nic_before=nic_snapshot())
    print(f'Demo logs: {out}', flush=True)
    from step9a_echoear import bench, verify, flash, check_firmware
    from step9a_assets import asset_server
    from step9a_support import host_address

    def interrupted(*_):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        with bench():
            result['nic_before'] = nic_snapshot()
            if args.flash:
                flash(out)
            port = verify(out)
            with demo_server(out) as bot, (nullcontext() if args.observe else asset_server()), DemoSerial(port, out, result['serial_events']) as link:
                board = link.board
                try:
                    cursor = board.cursor
                    board.reset()
                    board.wait_for('READY transport-console', 60, since=cursor)
                    result['boot_elf_sha256'] = check_firmware(board, cursor)
                    result['server_url'] = 'http://'+host_address()+':7861'
                    if args.observe:
                        result['observe_only']=True
                        board.wait_for('STATE Ready',120,since=cursor)
                    else:
                        result['display_setup'] = prepare_board(board, result['server_url'], out/'arena-budget.json')
                        cursor = board.cursor
                        board.command('connect', 90)
                        board.wait_for('STATE Ready', 5, since=cursor)
                        board.command('audio 1')
                    board.wait_for(r'MIC_GATE t=\d+ closed=0', 15, since=cursor)
                    result['bot_pid'] = getattr(bot, 'pid', None)
                    baseline = capture_demo_perf(board, out, 'unplug-before') if args.unplug_check else None
                    result['ready'] = True
                    (out/'session-start.json').write_text(json.dumps(result, indent=2)+'\n')
                    print('Demo ready: expressive cat listening. Speak first; Ctrl-C stops the session.', flush=True)
                    while True:
                        if bot.poll() is not None:
                            raise RuntimeError('Demo bot exited; see bot.log')
                        previous_events = len(link.events)
                        current = link.poll(reconnect=not args.observe)  # USB is optional after session startup.
                        if baseline and current is not None and any(e['kind']=='reattached' for e in link.events[previous_events:]):
                            try:
                                after = capture_demo_perf(current, out, f'unplug-after-{link.attempt}')
                                checks = unplug_counter_checks(baseline['perf'], after['perf'])
                                checks['serial_events'] = list(link.events)
                                checks['bot_pid'] = bot.pid
                                (out/'unplug-check.json').write_text(json.dumps(checks, indent=2)+'\n')
                                print('Unplug counter checks: '+str(checks['passed']), flush=True)
                            except (OSError, RuntimeError, TimeoutError) as exc:
                                (out/'unplug-check.json').write_text(json.dumps(dict(passed=False, error=str(exc)))+'\n')
                                print('Unplug PERF collection failed; demo remains running.', flush=True)
                        time.sleep(.25)
                finally:
                    board = link.poll(reconnect=False)
                    if args.observe:
                        result['cleanup_skipped']='Observe mode leaves autonomous board running'
                    elif board is None:
                        result['cleanup_skipped'] = 'Serial unreachable; no board commands sent'
                    else:
                        result['cleanup_errors'] = cleanup_board(board)
    except KeyboardInterrupt:
        result['interrupted'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        print(result['error'], flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        result['nic_after'] = nic_snapshot()
        before, after = result['nic_before']['interfaces'], result['nic_after']['interfaces']
        result['nic_byte_deltas'] = {name: {key: after[name][key]-before[name][key] for key in before[name]}
                                     for name in before.keys() & after.keys()}
        result['nic_basis'] = 'Total host NIC traffic; no non-board attribution or qualification verdict'
        (out/'session.json').write_text(json.dumps(result, indent=2)+'\n')
        print(f'Demo stopped; bench released. Transcripts: {out / "transcripts.log"}', flush=True)
    return 1 if result.get('error') or result['cleanup_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
