import json
from unittest.mock import Mock, patch
import pytest
from echoear_aec_bench import declared_boot_gain, boot, _VERIFIED_BOOT_GAINS, NORMAL_HASH


def test_declaration_required_and_consistent(tmp_path):
    with pytest.raises(ValueError, match='requires declared'):
        declared_boot_gain(tmp_path, 'candidate')
    assert declared_boot_gain(tmp_path, 'candidate', 15) == 15
    (tmp_path/'manifest.json').write_text(json.dumps({'boot_ref_gain_db': 24}))
    with pytest.raises(ValueError, match='disagree'):
        declared_boot_gain(tmp_path, 'candidate', 0)
    assert declared_boot_gain(tmp_path, 'candidate') == 24
    assert declared_boot_gain(tmp_path, NORMAL_HASH) is None


@pytest.mark.parametrize('line,ok', [('MIC3_GAIN=0x10 analog_db=0', True),
    ('MIC3_GAIN=0x18 analog_db=24', False), ('MIC3_GAIN=0x18 analog_db=0', False)])
def test_boot_checks_gain_and_register_and_closes_on_mismatch(tmp_path, line, ok):
    board = Mock()
    board.wait_for.side_effect = ['READY transport-console', 'elf_sha256=candidate', line]
    with patch.dict(_VERIFIED_BOOT_GAINS, {'candidate': 0}), patch('esp_serial.EspSerial', return_value=board):
        if ok:
            assert boot(tmp_path, 'candidate', 'candidate') is board
            assert json.loads((tmp_path/'candidate-boot-gain.json').read_text())['verified']
        else:
            with pytest.raises(ValueError, match='declared gain'):
                boot(tmp_path, 'candidate', 'candidate')
            board.close.assert_called_once()
