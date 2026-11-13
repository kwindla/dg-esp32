#!/usr/bin/env python3
"""One-shot historical witness: execute the original panel-init branches.

The permanent host gate needs only reviewed text fixtures. This optional tool
requires f5fc668's Git object, real pinned vendor drivers and an existing host
build for its CMake vendor cache. --record writes fixtures ONLY from the old
production source; candidate output is never an oracle.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = 'f5fc668ab4a52294b7c00f83bd6600b335fcbdfd'


def historical(path):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE+':'+path], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    original = historical('components/dg_display/bringup.cpp')
    begin = original.index('    esp_lcd_panel_dev_config_t panel_cfg{};')
    end = original.index('    ESP_LOGI("lcd-panel"', begin)
    # Keep the original #ifdef branches, statements, check calls and TE command.
    body = original[begin:end].replace('&panel)', 'panel)')
    source = '''#include "esp_lcd_spd2010.h"
#include "driver/gpio.h"
#include "dg_board/backlight.h"
#include "''' + str(HERE.parent/'backends/qspi/panel_init.h') + '''"
extern "C" esp_err_t dg_new_echoear_panel(esp_lcd_panel_io_handle_t,esp_lcd_panel_dev_config_t*,esp_lcd_panel_handle_t*);
namespace dg_display {
esp_err_t qspi_init_panel(esp_lcd_panel_io_handle_t io,esp_lcd_panel_handle_t* panel,PanelCensus observer){
unsigned before=0;
auto free_internal=[](){return 0u;};
auto checked=[&](const char* name,unsigned b,esp_err_t error){if(observer.after)observer.after(name,b,error);};
''' + body.replace('esp_lcd_panel_init(panel)', 'esp_lcd_panel_init(*panel)').replace('esp_lcd_panel_disp_on_off(panel,', 'esp_lcd_panel_disp_on_off(*panel,') + '''return ESP_OK;
}}
'''
    temporary = Path(os.environ.get('TMPDIR', str(Path.home()/'.cache/gap3-tmp')))
    temporary.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='panel-before-', dir=temporary) as work:
        work = Path(work)
        # Adapt only the old init branch to today's recording IO boundary.
        # Production headers no longer expose the legacy free-function API.
        compat = work/'compat/dg_board'; compat.mkdir(parents=True)
        (compat/'backlight.h').write_text('#pragma once\n#include "'+str(ROOT/'components/dg_board/include/dg_board/backlight.h')+'"\nnamespace dg_board {inline esp_err_t init_backlight(){return backlight::init();}}\n')
        (work/'old_init.cpp').write_text(source)
        (work/'echoear_panel.c').write_text(historical('components/dg_display/echoear_panel.c'))
        cmake = '''cmake_minimum_required(VERSION 3.22)
project(panel_before LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
'''
        for board in ('st77916_qspi', 'spd2010_qspi'):
            cmake += f'''add_executable(old_{board} "{HERE}/panel_trace.cpp" old_init.cpp echoear_panel.c
 "{args.build.resolve()}/panel-vendor/esp_lcd_st77916/esp_lcd_st77916.c"
 "{args.build.resolve()}/panel-vendor/esp_lcd_st77916/esp_lcd_st77916_spi.c"
 "{args.build.resolve()}/panel-vendor/esp_lcd_spd2010/esp_lcd_spd2010.c")
target_include_directories(old_{board} PRIVATE "{work}/compat" "{HERE}/trace_fakes" "{ROOT}/components/dg_board/include"
 "{args.build.resolve()}/panel-vendor/esp_lcd_st77916/include"
 "{args.build.resolve()}/panel-vendor/esp_lcd_st77916/priv_include"
 "{args.build.resolve()}/panel-vendor/esp_lcd_spd2010/include")
'''
            if board == 'st77916_qspi':
                cmake += f'target_compile_definitions(old_{board} PRIVATE CONFIG_DG_BOARD_ECHOEAR=1)\n'
        (work/'CMakeLists.txt').write_text(cmake)
        with (args.out/'historical-build.log').open('w') as log:
            subprocess.run(['cmake', '-S', str(work), '-B', str(work/'build')], stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(['cmake', '--build', str(work/'build'), '-j', '2'], stdout=log, stderr=subprocess.STDOUT, check=True)
        for board in ('st77916_qspi', 'spd2010_qspi'):
            result = subprocess.check_output([str(work/'build'/f'old_{board}')], text=True)
            (args.out/f'{board}-before.txt').write_text(result)
            fixture = HERE/'fixtures/panel-trace'/f'{board}.txt'
            if args.record:
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text(result)
            else:
                assert fixture.read_text() == result, board
            after = subprocess.check_output([str(args.build.resolve()/f'panel_trace_{board}')], text=True)
            (args.out/f'{board}-after.txt').write_text(after)
            assert after == result, f'{board} old vs new differs'
            print(f'{board}: original f5fc668 init trace equals candidate')


if __name__ == '__main__':
    main()
