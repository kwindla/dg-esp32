from pathlib import Path
from unittest.mock import Mock,patch

import pytest

from echoear_wifi_observation import run_hold,cleanup,sample_perf


@pytest.mark.parametrize('stage',['flash','collect','success'])
def test_restore_always_runs_in_owned_hold_after_any_acquisition_outcome(tmp_path,stage):
    flash=Mock(return_value='by-id')
    collect=Mock(return_value=dict(completed=True,qualification=False))
    restore=Mock(return_value=dict(verified=True))
    if stage=='flash':flash.side_effect=RuntimeError('partial flash')
    if stage=='collect':collect.side_effect=RuntimeError('raw fault')
    result=run_hold(tmp_path,dict(observer=dict(path='observer'),restore=dict(path='normal')),flash,collect,restore)
    restore.assert_called_once_with(Path('normal'),tmp_path/'restore-9936')
    assert result['restore']['verified'] and result['qualification'] is False and result['scoring_performed'] is False
    with pytest.raises(FileExistsError):run_hold(tmp_path,{},flash,collect,restore)
    restore.assert_called_once()


def test_failed_restore_retained_and_not_relabelled_as_success(tmp_path):
    result=run_hold(tmp_path,dict(observer=dict(path='o'),restore=dict(path='r')),
        Mock(return_value='port'),Mock(return_value={}),Mock(side_effect=RuntimeError('wrong MAC')))
    assert not result['restore']['verified'] and 'wrong MAC' in result['restore']['error']
    assert (tmp_path/'result.json').exists()


def test_cleanup_attempts_all_commands_after_failure():
    board=Mock();board.command.side_effect=[RuntimeError('first'),None,None,None]
    assert len(cleanup(board))==1
    assert board.command.call_count==4 and board.display_cleanup


def test_memory_or_lock_fault_stops_at_original_snapshot_without_extra_queries():
    value=dict(heap=dict(internal=dict(minimum_free=90000,largest=49000)),
               failed_allocations=dict(total=0,internal=0),display=dict(lock_failures=0,lock_timeouts=0))
    with patch('e2e_common.perf',return_value=value) as snapshot:
        assert sample_perf(Mock())==value;snapshot.assert_called_once()
    value['heap']['internal']['minimum_free']=80000
    with patch('e2e_common.perf',return_value=value):
        with pytest.raises(RuntimeError,match='resource guard'):sample_perf(Mock())
    value['heap']['internal']['minimum_free']=90000;value['display']['lock_failures']=1
    with patch('e2e_common.perf',return_value=value):
        with pytest.raises(RuntimeError,match='lock guard'):sample_perf(Mock())


def test_prepare_pins_external_arena_budget_and_rejects_later_change(tmp_path,monkeypatch):
    import json
    import echoear_wifi_observation as module
    evidence=tmp_path/'evidence';evidence.mkdir();image=tmp_path/'image';image.mkdir()
    metadata=dict(flags=dict(DG_WIFI_OBSERVE=True,DG_ECHOEAR_AEC=False,DG_BOARD_ECHOEAR=True,
        DG_DEMO_AUTOSTART=False),elf_sha256='observer',source_commit='2a6acb2')
    metadata['files']={}
    (image/'build-flags.json').write_text(json.dumps(metadata['flags']))
    (image/'manifest.json').write_text(json.dumps(metadata))
    folder=evidence/'item2-final-master-9bc2d42';folder.mkdir()
    budget=folder/'arena-budget.json';budget.write_text('{}')
    original=folder/'strict_voice_cat/attempt-2';original.mkdir(parents=True)
    prompts=[]
    for i in range(3):
        p=tmp_path/f'prompt{i}.wav';p.write_bytes(b'fixture');prompts.append(p)
    (original/'result.json').write_text(json.dumps(dict(runs=[dict(calibration=dict(prompt=dict(path=str(p)))) for p in prompts])))
    release=tmp_path/'release.txt';release.write_text('Released by A')
    monkeypatch.setattr(module,'EVIDENCE',evidence)
    monkeypatch.setattr(module,'bundle',lambda path,expected=None:dict(path=str(path),elf_sha256=expected))
    def git(args,**kwargs):return 'test-head\n' if kwargs.get('text') else b''
    with patch('subprocess.check_output',side_effect=git),patch('subprocess.run'):
        plan=module.prepare(image,tmp_path/'out',release)
    assert plan['budget']==str(budget) and str(budget) in plan['input_sha256']
    budget.write_text('{"changed":true}')
    with pytest.raises(ValueError,match='Frozen input changed'):module.verify_plan(tmp_path/'out/plan.json')


@pytest.mark.parametrize('late',['raw','reader','none'])
def test_restore_verification_requires_closed_log_and_reader_health(tmp_path,late):
    import echoear_wifi_observation as module
    from step9a_r2_checks import DisplayMonitor
    class Serial:
        def __init__(self,*args,log_path,**kwargs):
            self.log_path=log_path;self.cursor=0;self._error=None;self.display_monitor=DisplayMonitor()
            self.line='I (1342) dg-esp32: ESP32-S3 step 7 audio elf_sha256='+module.RESTORE_SHA
            log_path.write_text(self.line+'\n')
            self.display_monitor.feed((self.line+'\n').encode());self.display_monitor.line(self.line)
        def reset(self):pass
        def wait_for(self,*args,**kwargs):return self.line
        def command(self,*args,**kwargs):return 'OK'
        def __enter__(self):return self
        def __exit__(self,*args):
            if late=='raw':
                with self.log_path.open('a') as f:f.write('E (99) spi_master: late failure\n')
            if late=='reader':self._error=OSError('late disconnect')
    with patch('echoear_item2_campaign.flash_image',return_value='by-id'),patch('esp_serial.EspSerial',Serial):
        if late=='none':
            result=module.restore_image(tmp_path/'image',tmp_path/'restore')
            assert result['verified'] and result['closed_raw_audit']['passed']
        else:
            with pytest.raises(RuntimeError):module.restore_image(tmp_path/'image',tmp_path/'restore')
            assert not (tmp_path/'restore/result.json').exists()


@pytest.mark.parametrize('status', [dict(held=False,initializing=False,attempts=0),
    dict(held=True,initializing=True,attempts=0),dict(held=True,initializing=False,attempts=1)])
def test_autostart_must_be_held_before_any_automatic_attempt(status):
    import json
    from echoear_wifi_observation import hold_demo
    board=Mock();board.cursor=0;board.lines_since.return_value=[(1,0,'DEMO '+json.dumps(status))]
    with pytest.raises(RuntimeError,match='Autostart hold'):hold_demo(board)
    assert [x.args[0] for x in board.command.call_args_list]==['demo hold','demo status']


def test_demo_hold_does_not_mutate_persisted_configuration():
    import json
    from echoear_wifi_observation import hold_demo
    value=dict(held=True,initializing=False,attempts=0,enabled=True)
    board=Mock();board.cursor=0;board.lines_since.return_value=[(1,0,'DEMO '+json.dumps(value))]
    assert hold_demo(board)==value
    assert [x.args[0] for x in board.command.call_args_list]==['demo hold','demo status']
