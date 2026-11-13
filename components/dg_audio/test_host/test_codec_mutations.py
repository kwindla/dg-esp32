#!/usr/bin/env python3
"""Build isolated production mutations; require the same trace test to kill each."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def run(command, log):
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError('Mutation build/baseline failed: '+repr(command))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    build = args.build.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    gain = '    ESP_ERROR_CHECK(adc->set_mic_channel_gain(adc, ES7210_SEL_MIC1, float(profile.analog_gain_db)));\n'
    reference = '    ESP_ERROR_CHECK(adc->set_reg(adc,0x45,reference_gain_register(reference_gain_db)));\n'
    mutations = {
        'register-write-order': ('codec/es8311_es7210.cpp', lambda s: s.replace(gain, '').replace(reference, reference+gain)),
        'reference-slot-index': ('codec/es7210_slots.h', lambda s: s.replace('audio_sample(frame,map[2])', 'audio_sample(frame,map[1])')),
    }
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='step9-mutations-', dir=temporary) as work:
        work = Path(work)
        for name, (file, mutate) in mutations.items():
            source = work/name/'audio'
            shutil.copytree(ROOT.parent/'src', source/'src')
            path = source/'src'/file
            original = path.read_text()
            changed = mutate(original)
            assert original != changed, name+' did not match'
            path.write_text(changed)
            target = work/name/'build'
            with (args.out/(name+'-build.log')).open('w') as log:
                run(['cmake', '-S', str(ROOT), '-B', str(target), '-DAUDIO_TRACE_SOURCE_ROOT='+str(source),
                     '-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST='+str(build/'_deps/googletest-src')], log)
                run(['cmake', '--build', str(target), '--target', 'trace_echoear_aec_after', '-j', '2'], log)
            result = subprocess.run([sys.executable, str(ROOT/'test_codec_trace.py'),
                str(target/'trace_echoear_aec_after'), 'echoear_aec'], capture_output=True, text=True)
            (args.out/(name+'-test.log')).write_text(result.stdout+result.stderr)
            if result.returncode != 1 or 'golden' not in result.stderr or 'candidate' not in result.stderr:
                raise AssertionError(name+' survived or failed for an unrelated reason')
            print(name+': KILLED by golden trace comparison', flush=True)


if __name__ == '__main__':
    main()
