import pytest
from echoear_checks import codec_boot_evidence

@pytest.mark.parametrize('reference,reg',[(0,'58'),(1,'08')])
def test_boot_gate_uses_config_derived_expectation(reference,reg):
    line=f'startup verified REG0D=0x01 REG44=0x{reg} MIC1_GAIN=0x1a EXPECTED_REG44=0x{reg} no_dac_ref={reference}'
    assert codec_boot_evidence(line)['passed']
    assert not codec_boot_evidence(line+' I2C_If: Fail to write')['passed']
    assert not codec_boot_evidence(line.replace('REG0D=0x01','REG0D=0xfa'))['passed']
    assert not codec_boot_evidence(line.replace('MIC1_GAIN=0x1a','MIC1_GAIN=0x10'))['passed']

def test_missing_expected_value_and_mismatch_fail_closed():
    assert not codec_boot_evidence('startup verified REG0D=0x01 REG44=0x58 MIC1_GAIN=0x1a')['passed']
    assert not codec_boot_evidence('startup verified REG0D=0x01 REG44=0x08 MIC1_GAIN=0x1a EXPECTED_REG44=0x58 no_dac_ref=0')['passed']
