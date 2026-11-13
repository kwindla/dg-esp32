#!/usr/bin/env python3
"""Explicit acceptance mutation campaign; uses isolated sources, never edits worktree.

Run after the ordinary host gate. Logs/artifacts belong in the evidence tree.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MUTATIONS = (
    ('replication', 'src/present/logical_adapter.cpp', '+x/size_t(layout_.scale)', '+x%size_t(layout_.scale)', 'display_core_test', '--gtest_filter=Present.ScaleTwo*'),
    ('offset', 'src/present/logical_adapter.cpp', 'layout_.x+r.x0*layout_.scale', 'r.x0*layout_.scale', 'display_core_test', '--gtest_filter=Present.EightHundred*'),
    ('border', 'src/present/logical_adapter.cpp', '{0,0,info.w,layout_.y}', '{0,0,info.w,layout_.y+1}', 'display_core_test', '--gtest_filter=Present.EightHundred*'),
    ('st77916-command', 'backends/st77916_qspi/echoear_panel.c', '{0xF0, (uint8_t[]) {0x28}', '{0xF0, (uint8_t[]) {0x29}', 'panel_trace_st77916_qspi', 'st77916_qspi'),
    ('spd2010-qspi', 'backends/spd2010_qspi/panel_init.cpp', 'vendor.flags.use_qspi_interface=true', 'vendor.flags.use_qspi_interface=false', 'panel_trace_spd2010_qspi', 'spd2010_qspi'),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    for name, path, before, after, target, check in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix='present-mutation-', dir=temporary) as work:
            work = Path(work)
            component = work/'components/dg_display'
            shutil.copytree(HERE.parent, component)
            shutil.copytree(ROOT/'components/dg_board', work/'components/dg_board')
            shutil.copytree(ROOT/'components/dg_audio/include', work/'components/dg_audio/include')
            source = component/path
            text = source.read_text()
            assert text.count(before) == 1, (name, 'mutation anchor drift')
            source.write_text(text.replace(before, after))
            build = work/'build'
            with (args.out/f'{name}-build.log').open('w') as log:
                subprocess.run(['cmake', '-S', str(component/'test_host'), '-B', str(build),
                    '-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST='+str(args.build.resolve()/'_deps/googletest-src'),
                    '-DFETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON='+str(args.build.resolve()/'_deps/nlohmann_json-src'),
                    '-DDISPLAY_TRACE_VENDOR_ROOT='+str(args.build.resolve()/'panel-vendor')], stdout=log, stderr=subprocess.STDOUT, check=True)
                subprocess.run(['cmake', '--build', str(build), '--target', target, '-j', '2'], stdout=log, stderr=subprocess.STDOUT, check=True)
            if target == 'display_core_test':
                command = [str(build/target), check]
            else:
                command = ['python3', str(component/'test_host/test_panel_trace.py'), str(build/target), check]
            result = subprocess.run(command, capture_output=True, text=True)
            (args.out/f'{name}-test.log').write_text(result.stdout+result.stderr)
            if result.returncode == 0:
                raise AssertionError(f'{name}: mutation survived')
            print(f'{name}: killed (test exit {result.returncode})')


if __name__ == '__main__':
    main()
