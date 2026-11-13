"""Firmware-derived console vocabulary, without target/serial/tool access."""
from pathlib import Path
import pytest
from console_contract import ConsoleCommands, ROOT, literal_commands


def test_vocabulary_comes_from_real_console_dispatch():
    commands=ConsoleCommands.from_firmware()
    assert {'demo','touch-enable','touch-status','touch-inject','disp','connect','disconnect'}<=commands.names
    assert commands.demo_exact=={'hold','run','status'}
    assert commands.demo_prefix=={'configure '}
    for text in ('touch-enable 0','touch-enable 1','touch-status','touch-inject','demo hold','demo run','demo status',
                 'demo configure {"url":"http://example.invalid:7861","volume_db":-6,"enabled":true}'):
        commands.validate(text)


@pytest.mark.parametrize('command',['demo touch-enable 0','demo touch-status','demo touch-inject',
    'demo invalid','demo hold extra','demo','not-a-console-command'])
def test_fake_rejects_unknown_root_or_demo_action(command):
    with pytest.raises(ValueError,match='unsupported firmware'):ConsoleCommands.from_firmware().validate(command)


def test_parser_tracks_firmware_instead_of_a_duplicate_allow_list(tmp_path):
    (tmp_path/'main').mkdir()
    source=(ROOT/'main/console.cpp').read_text().replace('command,"touch-status"','command,"renamed-touch-status"')
    (tmp_path/'main/console.cpp').write_text(source)
    commands=ConsoleCommands.from_firmware(tmp_path)
    with pytest.raises(ValueError,match='unsupported firmware'):commands.validate('touch-status')
    commands.validate('renamed-touch-status')


@pytest.mark.parametrize('name',['p4_contention','p4_display','p4_audio','p4_audio_fix4','p4_headless','board_smoke','board_smoke_bounds'])
def test_current_product_helper_literal_commands_match_firmware(name):
    commands=ConsoleCommands.from_firmware();calls,_=literal_commands(ROOT/'hil'/(name+'.py'))
    for call in calls:
        try:commands.validate(call['text'],partial=call['partial'])
        except ValueError as exc:pytest.fail(f'{name}.py:{call["line"]}: {exc}')
