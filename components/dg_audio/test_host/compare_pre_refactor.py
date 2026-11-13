#!/usr/bin/env python3
"""Optional one-shot e27e428 witness; never invoked by CMake/CTest's host gate.

Snapshot old audio AND diagnostic headers into a temporary tree, compile only
those historical production sources with today's deterministic hardware fake,
and compare to the reviewed golden fixtures. Requires the base Git object.
"""
import argparse
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
BASE = 'e27e428e03d8b46041509cbf906be47e2ac2703b'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True, help='Existing host build (cached googletest)')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(['git', '-C', str(HERE.parents[2]), 'archive', BASE,
        'components/dg_audio/src', 'components/dg_audio/include', 'components/dg_diagnostics/include'], capture_output=True)
    if archive.returncode:
        raise SystemExit('Optional historical witness needs the e27e428 Git object in this checkout.')
    with tempfile.TemporaryDirectory(prefix='audio-pre-refactor-', dir=temporary) as work:
        work = Path(work)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as snapshot:
            snapshot.extractall(work/'snapshot', filter='data')
        # Historical production used the removed free-function names. Adapt
        # only this isolated hardware fake; no compatibility API is exported.
        compat = work/'compat'; compat.mkdir()
        (compat/'dg_board.h').write_text('#pragma once\n#include "dg_board/i2c.h"\nnamespace dg_board {inline esp_err_t init_bus(){return i2c::init();}inline i2c_master_bus_handle_t bus(){return i2c::bus();}}\n')
        audio = work/'snapshot/components/dg_audio'
        cmake = f'''cmake_minimum_required(VERSION 3.22)
project(historical_audio_witness LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
add_subdirectory("{HERE}" current EXCLUDE_FROM_ALL)
'''
        for board in ('echoear', 'echoear_aec', 'waveshare146'):
            cmake += f'''add_executable(old_{board} "{HERE}/trace_hardware.cpp"
 "{audio}/src/mic.cpp" "{audio}/src/speaker.cpp" "{audio}/src/echoear_codec.cpp"
 "{audio}/src/echoear_i2s.cpp" "{audio}/src/echoear_playback.cpp")
target_include_directories(old_{board} PRIVATE "{compat}" "{audio}/include" "{audio}/src"
 "{HERE}/trace_fakes" "{HERE}/fakes" "{work}/snapshot/components/dg_diagnostics/include")
target_link_libraries(old_{board} PRIVATE trace_vendor)
'''
            if board != 'waveshare146':
                cmake += f'target_compile_definitions(old_{board} PRIVATE CONFIG_DG_BOARD_ECHOEAR=1)\n'
            if board == 'echoear_aec':
                cmake += f'target_compile_definitions(old_{board} PRIVATE CONFIG_DG_ECHOEAR_AEC=1)\n'
        (work/'CMakeLists.txt').write_text(cmake)
        build = work/'build'
        with (args.out/'historical-build.log').open('w') as log:
            subprocess.run(['cmake', '-S', str(work), '-B', str(build),
                '-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST='+str(args.build.resolve()/'_deps/googletest-src')], stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(['cmake', '--build', str(build), '--target', 'old_echoear', 'old_echoear_aec', 'old_waveshare146', '-j', '2'], stdout=log, stderr=subprocess.STDOUT, check=True)
        for board in ('echoear', 'echoear_aec', 'waveshare146'):
            subprocess.run([sys.executable, str(HERE/'test_codec_trace.py'), str(build/('old_'+board)), board], check=True)


if __name__ == '__main__':
    main()
