"""EchoEar codec boot evidence; expected REG44 is emitted from firmware config."""
import re

def codec_boot_evidence(text, expected_mic_gain=0x1a):
    match=re.search(r'startup verified REG0D=0x([0-9a-f]+) REG44=0x([0-9a-f]+) MIC1_GAIN=0x([0-9a-f]+) EXPECTED_REG44=0x([0-9a-f]+) no_dac_ref=([01])',text)
    result=dict(available=bool(match),passed=False,i2c_error='I2C_If: Fail' in text)
    if match:
        system,reg44,mic,expected=[int(match[i],16) for i in range(1,5)]
        result.update(reg0d=system,reg44=reg44,mic1_gain=mic,expected_reg44=expected,no_dac_ref=bool(int(match[5])))
        result['passed']=system==0x01 and reg44==expected and mic==expected_mic_gain and not result['i2c_error']
    return result
