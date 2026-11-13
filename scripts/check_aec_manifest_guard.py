"""Regression for the approved AEC manifest guard, using IDF's real parser.

Run with IDF's Python. Waveshare omits AEC; P4 omits the EchoEar choice.
These cases must short-circuit missing keys without hiding genuine mistakes.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml
from idf_component_tools.debugger import KCONFIG_CONTEXT,SdkconfigContext
from idf_component_tools.manifest.if_parser import MissingKconfigError,parse_if_clause

ROOT=Path(__file__).resolve().parents[1]


class ManifestGuardTests(unittest.TestCase):
    def evaluate(self,rule,target,values):
        context=SdkconfigContext();context.sdkconfig.update(values)
        token=KCONFIG_CONTEXT.set(context)
        try:
            with patch.dict(os.environ,{'IDF_TARGET':target}):
                return parse_if_clause(rule).get_value()
        finally:KCONFIG_CONTEXT.reset(token)

    def test_actual_dependency_rules_with_omitted_board_and_aec_keys(self):
        dependencies=yaml.safe_load((ROOT/'components/dg_audio/idf_component.yml').read_text())['dependencies']
        for name in ('espressif/esp-sr','espressif/dl_fft','espressif/cjson'):
            rule=dependencies[name]['rules'][0]['if']
            for target,board,aec in [('esp32s3','echoear',False),('esp32s3','echoear',True),
                                    ('esp32s3','waveshare',False),('esp32p4','p4xc',False),('esp32p4','p4xc',True)]:
                with self.subTest(dependency=name,target=target,board=board,aec=aec):
                    values={}
                    if target=='esp32s3':values['DG_BOARD_ECHOEAR']=board=='echoear'
                    if board!='waveshare':values['DG_ECHOEAR_AEC']=aec
                    self.assertEqual(self.evaluate(rule,target,values),aec)

    def test_unguarded_rule_reproduces_missing_key_failure(self):
        with self.assertRaises(MissingKconfigError):
            self.evaluate('$CONFIG{DG_ECHOEAR_AEC} == True','esp32s3',{'DG_BOARD_ECHOEAR':False})

    def test_missing_aec_on_an_enabled_board_is_not_silently_ignored(self):
        dependencies=yaml.safe_load((ROOT/'components/dg_audio/idf_component.yml').read_text())['dependencies']
        for name in ('espressif/esp-sr','espressif/dl_fft','espressif/cjson'):
            with self.subTest(dependency=name),self.assertRaises(MissingKconfigError):
                self.evaluate(dependencies[name]['rules'][0]['if'],'esp32s3',{'DG_BOARD_ECHOEAR':True})


if __name__=='__main__':unittest.main()
