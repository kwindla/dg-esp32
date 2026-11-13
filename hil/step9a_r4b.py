#!/usr/bin/env python3
"""R4b's two-run waiver; original R4 arms/evidence are never rewritten."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from alsa import record_room_baseline
from esp_serial import EspSerial, ROOT, port_lock
from step7_support import BotServer
from step8e2b_analysis import lcd_memory_gate
from step9a_assets import asset_server
from step9a_r2_checks import audit
from step9a_r2_diagnose import save
from step9a_r3b_natural import Observer, delta, settle
from step9a_r4_inventory import crypto, difference
from step9a_support import FIXTURES, host_address, pack, read
from transport_test import READY_LOG

OUT = Path('/home/khkramer/src/evidence/step9a/r4b')
REPORT = ROOT/'proj-2026-10-08-0949/codex-jobs/step-9a-r4b-report.md'
IMAGE = OUT/'image-production/dg-esp32.elf'
CENSUS_NAME = os.environ.get('STEP9A_R4B_CENSUS_NAME', 'census-production')
# Scan every byte, including malformed lines. WDT is specifically not a 9a stop.
DRIVER_ERROR = re.compile(rb'DMA TX underflow|E spi_master|E\s*\([^\r\n)]*\)\s*(?:spi_master|spi_flash|gdma|esp_lcd|lcd|display(?:-flash)?|esp_crypto|esp_aes|esp_sha)\b')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def baseline(path, name):
    value = record_room_baseline(path, name)
    value['acoustic_waived'] = True
    value['waiver'] = 'step-9a-r4b only: fixture mic and offline bot; not C/SHA/performance/step7'
    save(path/'result.json', value)
    return value


def freeze():
    OUT.mkdir(exist_ok=True)
    image = IMAGE.parent
    image.mkdir(exist_ok=False)
    before = json.loads(Path('/home/khkramer/src/evidence/step9a/r4/review-amendment/metadata.json').read_text())
    for name, expected in before['image'].items():
        assert sha(ROOT/'build'/name) == expected
        shutil.copy2(ROOT/'build'/name, image/name)
    files = set(before['files'])
    files.update(json.loads(Path('/home/khkramer/src/evidence/step9a/r4/candidate-metadata.json').read_text())['files'])
    files.update(('hil/step9a_r4b.py', 'server/bot.py'))
    sources = {}
    for name in sorted(files):
        target = OUT/'source-production'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, target)
        sources[name] = sha(target)
    save(OUT/'production-freeze.json', {'image':before['image'], 'sources':sources,
        'old_A_B_elf':before['board_image_retained'], 'A_B_rerun':False,
        'review_decides_A_B_image_change':True, 'acoustic_waiver_scope':'two R4b runs and their required image census only'})
    save(OUT/'part1-predeclared.json', {'elf':before['image']['dg-esp32.elf'],
        'seconds':300, 'message_bytes':1024, 'rate':'same bounded bot SCTP backpressure path as inventory-flood',
        'cat_hz':30, 'spi_hz':40000000, 'guard_default':1, 'mic_fixture':1,
        'driver_regex':DRIVER_ERROR.pattern.decode(), 'memory_partial_fields':'independently valid JSON heap/heap_window/failed_allocations subobjects only; never repair a full PERF',
        'stops':['DMA error', 'raw driver error', 'priority key', 'coverage loss', 'undrained flood', 'memory failure/incomplete windows', 'cleanup failure'],
        'findings_only':['watchdog', 'render liveness', 'malformed PERF', 'fps', 'message/log drops'],
        'retakes':False, 'port':7861})


def env():
    os.environ['OFFLINE_SKIP_GREETING'] = '1'
    os.environ['OFFLINE_IDLE_TIMEOUT_SECS'] = '7200'
    os.environ['OFFLINE_R4_CIPHER_AUDIT'] = '1'
    os.environ.pop('OFFLINE_R4_FLOOD_SECONDS', None)


def perf_records(data, allow_partial=False):
    records, malformed, partial = [], [], []
    for number, raw in enumerate(data.decode(errors='replace').splitlines(), 1):
        if not raw.startswith('PERF '):
            continue
        try:
            value = json.loads(raw[5:])
        except ValueError as exc:
            malformed.append({'line':number, 'reason':str(exc)})
            if not allow_partial:
                continue
            # A ROM WDT printf may interrupt a PERF. Do not remove any bytes or
            # manufacture a complete row. Independently intact memory objects
            # are usable for the specifically amended R4b memory criterion.
            value = {}
            try:
                for key in ('device_us', 'heap', 'heap_window', 'failed_allocations'):
                    positions = list(re.finditer(r'"'+key+r'"\s*:', raw))
                    if len(positions) != 1:
                        raise ValueError('Ambiguous/missing memory field '+key)
                    value[key], _ = json.JSONDecoder().raw_decode(raw[positions[0].end():].lstrip())
                assert isinstance(value['device_us'], int)
                assert all(isinstance(value[k], dict) for k in ('heap','heap_window','failed_allocations'))
            except (ValueError, AssertionError):
                continue
            partial.append({'line':number, 'fields':list(value), 'full_PERF_reconstructed':False})
        records.append(value)
    return records, malformed, partial


def cleanup(obs, result):
    b = obs.board
    b.display_cleanup = True
    settle(obs, .35)
    # R4b never arms the SHA worker. Its stop command requires diagnostic mode
    # and must not be sent during the production census; verify it is off below.
    for cmd in ('audio 0', 'disconnect', 'disp bench off',
                'mic-fixture 0', 'gate auto', 'disp defer-persistence 0', 'disp call-persistence 0'):
        try:
            obs.command(cmd, 45)
        except Exception as exc:
            result.setdefault('cleanup_errors', []).append(str(exc))
    settle(obs, .35)
    try:
        obs.stats()
        result['cleanup_info'] = obs.info()
        result['cleanup_status'] = read(b, 'status', 'DISPLAY')
        obs.scan()
        result['cleanup_crypto'] = crypto(obs)
        result['cleanup_hash_worker'] = read(b, 'r5-hash-status', 'DISPLAY_R5_HASH')
        obs.scan()
        status = result['cleanup_status']; worker = result['cleanup_hash_worker']
        result['cleanup_verified'] = (status['bench'][0] == 0 and status['queue'] == [0, 0]
            and not result['cleanup_info']['dead'] and not status['failed']
            and not worker['running'] and not worker['allocations_live']
            and status['flash']['count'] == 16 and status['flash']['used'] == 520192
            and status['flash']['temp_bytes'] == 0)
    except Exception as exc:
        result.setdefault('cleanup_errors', []).append(str(exc))


def census():
    env()
    path = OUT/CENSUS_NAME
    path.mkdir(exist_ok=False)
    r = {'completed':False, 'acoustic_waived':True, 'elf_sha256':sha(IMAGE),
         'name':CENSUS_NAME, 'label':'harness-fault rerun' if CENSUS_NAME == 'census-production-02' else 'original',
         'retained_failed_parent':'census-production' if CENSUS_NAME == 'census-production-02' else None}
    try:
        r['flash_baseline'] = baseline(path/'flash-room', 'R4b required production census flash')
        with port_lock(), (path/'flash.log').open('x') as log:
            subprocess.run(['/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python', '-m', 'esptool', '--chip', 'esp32s3', '-p', '/dev/ttyACM0', '-b', '460800', '--before', 'default-reset', '--after', 'hard-reset', 'write-flash', '0x10000', str(IMAGE.with_suffix('.bin'))], stdout=log, stderr=subprocess.STDOUT, check=True, cwd=ROOT)
        r['reset_baseline'] = baseline(path/'reset-room', 'R4b required production census reset')
        with EspSerial('/dev/ttyACM0', log_path=path/'serial.log', display_checks=True) as b:
            obs = Observer(b, path)
            try:
                at = b.cursor
                b.reset(); b.wait_for('READY transport-console', 60, since=at)
                assert r['elf_sha256'] in b.wait_for('elf_sha256=', 3, since=at)
                r['boot_crypto'] = crypto(obs)
                assert r['boot_crypto']['guard'] == 1
                for cmd in ('quiet 1', 'audio 0', 'mic-fixture 1', 'disp clock 40000000', 'server-url http://'+host_address()+':7861', 'disp init', 'disp origin http://'+host_address()+':7861'):
                    obs.command(cmd)
                fixture = json.loads((FIXTURES/'fetch-fixture.json').read_text())
                with asset_server(port=7861):
                    at = b.cursor
                    obs.command('disp probe http://'+host_address()+':7861/'+fixture['hash']+'.dgi')
                    b.wait_for('probe_complete ok=1 bytes=306917', 45, since=at)
                obs.stats()
                with BotServer(path/'bot', port=7861):
                    for _ in range(2):
                        at = b.cursor
                        obs.command('connect', 90); b.wait_for(READY_LOG, 45, since=at)
                        obs.stats(); settle(obs, 10); obs.stats(); settle(obs, .35)
                        obs.command('disconnect', 45); settle(obs, .35); obs.stats()
                r['completed'] = True
            except Exception as exc:
                r['error'] = repr(exc)
            finally:
                cleanup(obs, r)
                save(path/'before-close.json', r)
        raw = (path/'serial.log').read_bytes()
        r['snapshots'], r['malformed'], _ = perf_records(raw)
        r['memory'] = lcd_memory_gate(r['snapshots'], reconnects=1)
        r['internal_cost'] = 113624-min(s['heap']['internal']['minimum_free'] for s in r['snapshots'])
        r['monitor'] = audit(raw, diagnostic=True)
        r['ready_dump'] = [json.loads(l.split(' ',1)[1]) for l in raw.decode(errors='replace').splitlines() if l.startswith('DISPLAY_R4_GDMA_READY ')]
        r['passed'] = bool(r['completed'] and r['memory']['passed'] and r['internal_cost'] <= 27108 and r['monitor']['passed'] and not r['malformed'] and not r.get('cleanup_errors') and r.get('cleanup_verified') and len(r['ready_dump']) == 1 and len(r['ready_dump'][0]['pairs']) == 5)
        if r['passed']:
            F = min(s['heap']['psram']['window_session_minimum_free'] for s in r['snapshots'] if s['heap_window']['session_samples'])
            largest = min(s['heap']['psram']['sampled_largest_minimum'] for s in r['snapshots'] if s['heap_window']['session_samples'])
            W = 1287760
            maximum = min(F-W-589824, largest)
            budget = min(491520, maximum//4096*4096)
            assert budget >= 106895+306917+2048
            save(OUT/'arena-budget.json', {'F':F, 'W':W, 'largest':largest, 'budget':budget, 'maximum':maximum, 'basis':'fresh amended-image full-window no-arena census; old W retained'})
    except Exception as exc:
        r['error'] = repr(exc)
        r['passed'] = False
    finally:
        save(path/'result.json', r)
    print(json.dumps({'census_passed':r['passed'], 'internal_cost':r.get('internal_cost'), 'memory':r.get('memory'), 'error':r.get('error')}), flush=True)
    return r


def cat_setup(obs):
    budget = json.loads((OUT/'arena-budget.json').read_text())
    obs.command('disp init')
    obs.command('disp origin http://'+host_address()+':7861')
    obs.command('disp arena {budget} {F} {W} {largest}'.format(**budget))
    images = pack()[:16]
    with asset_server(port=7861):
        for i, image in enumerate(images):
            obs.durable('load', {'hash':image['hash'], 'bytes':image['bytes'], 'url':'http://'+host_address()+':7861/'+image['hash']+'.dgi'}, 'r4b-fixture-'+str(i))
            obs.command('disp asset '+str(i)+' '+image['hash'])
    return {'budget':budget, 'images':images, 'status':read(obs.board, 'status', 'DISPLAY')}


def flood():
    assert json.loads((OUT/CENSUS_NAME/'result.json').read_text())['passed'], 'Required strict census must pass'
    env()
    os.environ['OFFLINE_R4_FLOOD_SECONDS'] = '300'
    path = OUT/'part1-flood'
    path.mkdir(exist_ok=False)
    r = {'completed':False, 'acoustic_waived':True, 'elf_sha256':sha(IMAGE)}
    r['room_baseline'] = baseline(path/'room-baseline', 'R4b part1 flood')
    with EspSerial('/dev/ttyACM0', log_path=path/'serial.log', display_checks=True, display_diagnostic=True) as b:
        obs = Observer(b, path); b.display_terminal_events = True
        try:
            at = b.cursor
            b.reset(); b.wait_for('READY transport-console', 60, since=at)
            assert r['elf_sha256'] in b.wait_for('elf_sha256=', 3, since=at)
            for cmd in ('quiet 1', 'audio 0', 'disp clock 40000000', 'server-url http://'+host_address()+':7861'):
                obs.command(cmd)
            r['setup'] = cat_setup(obs)
            assert crypto(obs)['guard'] == 1
            for cmd in ('disp r2 diagnostic 1', 'peer-option call-timing 1', 'peer-option arrival-trace 1', 'mic-fixture 1', 'gate open', 'audio 1', 'disp bench cat 30'):
                obs.command(cmd)
            settle(obs, 2); obs.stats()
            with BotServer(path/'bot', port=7861) as bot:
                at = b.cursor
                obs.command('connect', 90); b.wait_for(READY_LOG, 45, since=at)
                r['before'] = obs.info(); r['crypto_before'] = crypto(obs)
                deadline = time.monotonic()+345
                while True:
                    events = bot.events()
                    if any(e.get('event') == 'r4-flood-failed' for e in events):
                        raise RuntimeError('Bot flood failed')
                    if any(e.get('event') == 'r4-flood-complete' for e in events):
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('Bot flood completion timeout')
                    settle(obs, 1); obs.stats()
                    info = obs.info()
                    if info['dma_errors'] or info['dead'] or info['spi_coverage_errors']:
                        raise RuntimeError('9a DMA/dead/coverage stop')
                    if any(e.get('display_failure') == 'lcd_tx_priority_applied' for e in b.display_monitor.errors):
                        raise RuntimeError('9a priority hard-fail')
                    if DRIVER_ERROR.search((path/'serial.log').read_bytes()):
                        raise RuntimeError('9a raw driver-error stop')
                r['flood_events'] = [e for e in bot.events() if str(e.get('event','')).startswith('r4-flood-')]
                r['after'] = obs.info(); r['crypto_after'] = crypto(obs); obs.stats()
                r['exposure'] = delta(r['before'], r['after'])
                r['crypto_delta'] = difference(r['crypto_before'], r['crypto_after'])
                r['completed'] = True
                cleanup(obs, r)
        except Exception as exc:
            r['error'] = repr(exc)
        finally:
            if 'cleanup_info' not in r:
                cleanup(obs, r)
            save(path/'before-close.json', r)
    raw = (path/'serial.log').read_bytes()
    snapshots, malformed, partial = perf_records(raw, allow_partial=True)
    r['memory'] = lcd_memory_gate(snapshots, reconnects=1)
    r['memory_partial_subobjects'] = partial
    r['monitor_all_findings'] = audit(raw, diagnostic=True)
    r['driver_errors'] = [{'offset':m.start(), 'match':m.group().decode(errors='replace')} for m in DRIVER_ERROR.finditer(raw)]
    r['priority_failures'] = [e for e in r['monitor_all_findings']['errors'] if e.get('display_failure') == 'lcd_tx_priority_applied']
    r['malformed_PERF'] = malformed
    r['watchdog_records'] = raw.count(b'task_wdt: Task watchdog got triggered')
    r['watchdog_error_fragments'] = raw.count(b'task_wdt:')
    r['render_liveness_findings'] = [e for e in r['monitor_all_findings']['errors'] if 'display_liveness' in e]
    r['message_queue_drops'] = max((s.get('drop_reasons',{}).get('message_queue',0) for s in snapshots), default=0)
    r['log_drops'] = max((s.get('diagnostic_log_drops',0) for s in snapshots), default=0)
    done = next((e for e in r.get('flood_events',[]) if e['event'] == 'r4-flood-sending-complete'), {})
    drain = next((e for e in r.get('flood_events',[]) if e['event'] == 'r4-flood-complete'), {})
    c = r.get('cleanup_crypto', {})
    r['checks'] = {'completed':r['completed'], 'dma_zero':r.get('cleanup_info',{}).get('dma_errors') == 0,
        'raw_driver_clean':not r['driver_errors'], 'priority_applied':c.get('lcd_tx_captured') is True and c.get('last_priority_written') == 1 and c.get('priority_applies',0) >= 1 and c.get('lcd_connects') == 1 and not r['priority_failures'],
        'coverage_zero':c.get('coverage_lost') == 0 and c.get('gate_failures') == 0 and r.get('cleanup_info',{}).get('spi_coverage_errors') == 0,
        'flood_drained':done.get('elapsed_s',0) >= 300 and drain.get('pending_bytes') == 0,
        'memory_clean':r['memory']['passed'] is True,
        'cleanup_verified':r.get('cleanup_verified',False) and not r.get('cleanup_errors') and not obs.parse_failures and not obs.telemetry_failures}
    r['passed_9a'] = all(r['checks'].values())
    save(path/'result.json', r)
    with REPORT.open('a') as f:
        f.write('\n## Progress note after part 1\n\n')
        f.write(f"Fresh census {CENSUS_NAME}: {json.loads((OUT/CENSUS_NAME/'result.json').read_text()).get('internal_cost')} B internal cost; production ELF {r['elf_sha256']}. Acoustic waiver recorded with baseline {r['room_baseline']['baseline_dbfs']:.6f} dBFS. Flood completed={r['completed']}; 9a pass={r['passed_9a']}; checks={r['checks']}. Exposure={r.get('exposure')}; source={done}; watchdog episodes/fragments={r['watchdog_records']}/{r['watchdog_error_fragments']}; malformed PERF={len(malformed)}, render liveness findings={len(r['render_liveness_findings'])}; message/log drops={r['message_queue_drops']}/{r['log_drops']}. Crypto deltas retained in part1-flood/result.json. These CPU/parser/log findings are not display-DMA failures. No retake or completed A/B rerun.\n")
    print(json.dumps({'part1_passed_9a':r['passed_9a'], 'checks':r['checks'], 'exposure':r.get('exposure'), 'error':r.get('error')}), flush=True)
    return r


def main():
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['freeze','census','flood']); args = p.parse_args()
    if args.action == 'freeze':
        freeze(); return 0
    r = census() if args.action == 'census' else flood()
    return 0 if r.get('passed', r.get('passed_9a')) else 2


if __name__ == '__main__':
    raise SystemExit(main())
