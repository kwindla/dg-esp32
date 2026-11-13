"""Placement/guard regressions; no hardware, services or managed-file edits."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import re
import shutil
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('aec_image',ROOT/'scripts/check-aec-image.py')
image=importlib.util.module_from_spec(spec);spec.loader.exec_module(image)


class AecBuildTests(unittest.TestCase):
    def test_enabled_wrapper_cannot_compile_without_heap_fail_stop(self):
        audio=ROOT/'components/dg_audio'
        command=[shutil.which('c++') or 'c++','-std=c++17','-fsyntax-only',
            '-DCONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS=0',
            '-I'+str(audio/'test_host/aec_fakes'),'-I'+str(audio/'include'),str(audio/'src/aec.cpp')]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('AEC requires heap fail-stop',result.stderr)

    def test_manifest_short_circuits_hidden_aec_symbol_on_legacy_board(self):
        from idf_component_tools.manifest.if_parser import parse_if_clause
        text=(ROOT/'components/dg_audio/idf_component.yml').read_text()
        rules=re.findall(r'- if: "([^"]+)"',text)
        self.assertEqual(len(rules),3)
        for values,expected in [({'DG_BOARD_ECHOEAR':False},False),
                                ({'DG_BOARD_ECHOEAR':True,'DG_ECHOEAR_AEC':False},False),
                                ({'DG_BOARD_ECHOEAR':True,'DG_ECHOEAR_AEC':True},True)]:
            context=SimpleNamespace(get=lambda:SimpleNamespace(sdkconfig=values))
            with patch('idf_component_tools.manifest.if_parser.KCONFIG_CONTEXT',context):
                for rule in rules:self.assertIs(parse_if_clause(rule).get_value(),expected)

    def test_original_bytes_in_dram_or_partial_mapping_fail(self):
        contents=bytes(range(256))*12
        image.verify_table('.flash.rodata',0x3c010000,3072,contents,contents)
        cases=[('.dram0.data',0x3fca1454,3072,contents),
               ('.flash.rodata',0x3fca1454,3072,contents),
               ('.flash.rodata',0x3dfff800,3072,contents),
               ('.flash.rodata',0x3c010000,3071,contents),
               ('.flash.rodata',0x3c010000,3072,contents[:-1]+b'\x00')]
        for case in cases:
            with self.assertRaises(ValueError):image.verify_table(*case,contents)

    def test_changed_vendor_archive_fails_closed_only_when_aec_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);library=directory/'lib/esp32s3';library.mkdir(parents=True)
            (library/'libesp_audio_processor.a').write_bytes(b'unaudited archive')
            script=directory/'guard.cmake'
            script.write_text('function(idf_component_get_property result component property)\n'
                '  set(${result} "'+str(directory)+'" PARENT_SCOPE)\nendfunction()\n'
                'include("'+str(ROOT/'cmake/echoear_aec_memory.cmake')+'")\n')
            disabled=subprocess.run(['cmake','-DCONFIG_DG_ECHOEAR_AEC=OFF','-P',str(script)],capture_output=True,text=True)
            self.assertEqual(disabled.returncode,0,disabled.stderr)
            enabled=subprocess.run(['cmake','-DCONFIG_DG_ECHOEAR_AEC=ON','-P',str(script)],capture_output=True,text=True)
            self.assertNotEqual(enabled.returncode,0)
            self.assertIn('requires re-auditing',enabled.stderr)


if __name__=='__main__':unittest.main()
