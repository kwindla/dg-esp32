"""No devices: fixed-bound product evidence and fail-closed console regressions."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import p4_audio as audio
import board_smoke as smoke
from test_wire_contract import capture, serialize, IDENTITY
from wire_contract import ContractError


class Console:
    def __init__(self, *, cache=True, gain=True, aec=False, reason='aec-disabled'):
        self.rows=[]; self.sent=[]; self.cache=cache; self.gain=gain; self.aec=aec; self.reason=reason
    @property
    def cursor(self): return len(self.rows)
    def send(self, command):
        self.sent.append(command); seq=len(self.sent); ok=True
        if command.startswith('cache-counters') and not self.cache:
            self.rows.append('unsupported build=cache-counters-disabled cmd=cache-counters'); ok=False
        elif command.startswith('mic-analog'):
            if self.gain:self.rows.append('I (100) echoear-audio: MIC_GAIN_PATH analog_db=30 reg=0x1a digital_percent=0')
        elif command.startswith('aec-'):
            ok=self.aec
            if not ok:self.rows.append('unsupported build='+self.reason+' cmd=aec-ref-gain-status')
        self.rows.append(f'{"OK" if ok else "ERR"} #{seq} {command.split()[0]}')
        return seq
    def wait_for(self, pattern, timeout, since): return self.rows[-1]
    def lines_since(self, at): return [(i+1,0,line) for i,line in enumerate(self.rows) if i>=at]


@pytest.mark.parametrize('cache',[False,True])
def test_console_enabled_and_disabled_cache_and_aec_rejection(cache):
    board=Console(cache=cache)
    assert audio.console_check(board)['checks_passed']
    assert board.sent==['cache-counters 0','mic-analog-gain 30','aec-ref-gain-status','ping']


@pytest.mark.parametrize('options',[dict(gain=False),dict(aec=True),dict(reason='wrong')])
def test_console_missing_gain_readback_or_wrong_aec_result_fails(options):
    with pytest.raises(ContractError): audio.console_check(Console(**options))


@pytest.fixture
def pair(tmp_path):
    paths=[]
    for board,target in [('echoear','esp32s3'),('p4xc','esp32p4')]:
        path=tmp_path/board;path.mkdir();paths.append(path)
        value=capture(target,False)
        rows=serialize(value)
        (path/'main-serial.log').write_text('\n'.join(r for r in rows if not r.startswith('DEMO_CAPABILITY'))+'\nPHASE closed\n')
        (path/'main-bot.log').write_text('\n'.join(r for r in rows if r.startswith('DEMO_CAPABILITY'))+'\n')
        (path/'identity.json').write_text(json.dumps(IDENTITY))
        (path/'result.json').write_text(json.dumps(dict(acquisition_passed=True,board=board,variant='normal',
                                                       calibration=True,headless=True,identity=IDENTITY,restore=dict(clean=True,action='keep-tested-p4'))))
    return *paths,tmp_path/'pair.json'


def test_pair_enforces_product_and_fixed_bounds_after_headless_calibration(pair):
    left,right,out=pair
    result=audio.product_pair(left,right,out)
    assert result['checks_passed'] and not result['qualification'] and result['profile']=='product'
    assert result['bounds_sha256']==audio.digest(smoke.BOUNDS_FILE)
    assert len(result['measured'])==2


@pytest.mark.parametrize('mutation',['capability','opus','intervals','stall','trailing','identity','acquisition'])
def test_pair_rejects_missing_product_records_stalls_and_identity_changes(pair,mutation):
    left,right,out=pair
    serial=right/'main-serial.log';text=serial.read_text()
    if mutation=='capability': (right/'main-bot.log').write_text('')
    elif mutation=='opus': text='\n'.join(r for r in text.splitlines() if 'opus_enc=' not in r)
    elif mutation=='intervals': text='\n'.join(r for r in text.splitlines() if not r.startswith('UPLINK_INTERVALS'))
    elif mutation=='stall': text=text.replace('max_us=20000','max_us=999999')
    elif mutation=='trailing': text=text.replace('last_ok_age_us=20000','last_ok_age_us=999999')
    elif mutation=='identity': (right/'identity.json').write_text(json.dumps(dict(IDENTITY,bot_instance='restarted')))
    else:
        path=right/'result.json';value=json.loads(path.read_text());value['acquisition_passed']=False;path.write_text(json.dumps(value))
    serial.write_text(text)
    with pytest.raises(ContractError): audio.product_pair(left,right,out)
    assert not out.exists()


def test_live_uses_frozen_restore_guard_and_keeps_successful_p4_only(tmp_path,monkeypatch):
    events=[]
    class Guard:
        success=False;record=None
        def touch(self): events.append('touch')
        def release_signals(self): pass
        def __enter__(self): return self
        def __exit__(self,*exc):
            events.append('keep' if self.success else 'restore');self.record=dict(clean=True,action=events[-1])
    class Link:
        def __enter__(self): return self
        def __exit__(self,*exc): pass
    class IO:
        def __init__(self,*args): self.guard=Guard()
        def preflight_tools(self): events.append('tools')
        def preflight_restore(self): events.append('preflight')
        def hold(self): return self.guard
        def flash(self,*args): events.append('flash')
        def open(self): return Link()
        def boot(self,*args): events.append('boot')
    monkeypatch.setattr(audio,'HardwareIO',IO)
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (dict(profile='normal',aec_enabled=False,elf_sha256='a'*64),{}))
    monkeypatch.setattr(audio,'digest',lambda path:'b'*64)
    monkeypatch.setattr(audio,'target_lock',lambda *args:'c'*64)
    monkeypatch.setattr(audio,'audio_settings',lambda *args: {})
    monkeypatch.setattr(audio,'verify_audio',lambda *args: dict(analog_db=30,digital_percent=125,playback_volume_db=-6))
    monkeypatch.setattr(audio,'preflight_acoustic',lambda *args: events.append('room'))
    monkeypatch.setattr(audio,'acoustic',lambda *args: dict(acquisition_passed=True))
    args=SimpleNamespace(bundle=tmp_path/'bundle',board='p4xc',out=tmp_path/'good',action='acoustic')
    assert audio.live(args)==0
    assert events==['tools','preflight','room','touch','flash','boot','keep']
    assert json.loads((args.out/'result.json').read_text())['restore']['action']=='keep'
    events.clear();args.out=tmp_path/'bad'
    def failed(*args): raise RuntimeError('capture incomplete')
    monkeypatch.setattr(audio,'acoustic',failed)
    with pytest.raises(RuntimeError,match='incomplete'): audio.live(args)
    assert events[-1]=='restore'
    assert not json.loads((args.out/'result.json').read_text())['acquisition_passed']
    events.clear();args.out=tmp_path/'noisy'
    monkeypatch.setattr(audio,'preflight_acoustic',failed)
    with pytest.raises(RuntimeError,match='incomplete'): audio.live(args)
    assert events==['tools','preflight']  # No hold/touch/flash/restore.
    assert json.loads((args.out/'result.json').read_text())['restore'] is None


def test_live_requires_real_runner_lock_before_io(tmp_path):
    import os
    if not os.environ.get('DG_BENCH_LOCK_FD'):
        with pytest.raises(RuntimeError,match='bench'):
            audio.HardwareIO(SimpleNamespace(out=tmp_path),{})


def cost_fixture(path):
    from test_board_smoke import perf
    path.mkdir()
    (path/'result.json').write_text(json.dumps(dict(board='p4xc',variant='normal',acquisition_passed=True,restore=dict(clean=True,action='keep-tested-p4'))))
    row=perf()
    row.update(encode_frames=600,speech_decode_frames=200,decode_frames=210,uplink_frame_ms=20,
        encode_p50_us=4000,encode_p95_us=5000,encode_max_us=6000,
        decode_p50_us=500,decode_p95_us=700,decode_max_us=900,
        speech_decode_p50_us=600,speech_decode_p95_us=700,decode_errors=0,
        tx_stack=6000,decode_stack=6000,owner_stack=6000,rx_stack=6000,
        generation=2,codec_histogram_bucket_us=100,per_core_idle_percent=[60,50])
    for index in (1,2): (path/f'perf-{index:03d}.json').write_text(json.dumps(row))
    return row


def test_live_costs_report_real_session_work_and_exact_max(tmp_path):
    directory=tmp_path/'run';cost_fixture(directory)
    row=audio.session_costs(directory,tmp_path/'costs.json')
    assert row['checks_passed'] and not row['qualification']
    assert row['metrics']['encode_max_us']==6000
    assert row['resources']['minimum_internal']==100000


@pytest.mark.parametrize('key,value',[('speech_decode_frames',0),('encode_frames',499),('tx_stack',4095),
                                      ('decode_errors',1),('uplink_frame_ms',40)])
def test_live_costs_reject_silence_only_missing_work_and_bad_resources(tmp_path,key,value):
    directory=tmp_path/'run';row=cost_fixture(directory);row[key]=value
    for path in directory.glob('perf-*'):path.write_text(json.dumps(row))
    with pytest.raises(ContractError):audio.session_costs(directory,tmp_path/'costs.json')



def test_target_lock_uses_image_provenance_even_when_checkout_lock_differs(tmp_path):
    manifest=dict(elf_sha256='a'*64,dependencies_lock_sha256='b'*64)
    assert audio.target_lock(tmp_path,manifest,'echoear')=='b'*64
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    row=dict(board='p4xc',target_dependencies_lock='dependencies.p4xc.lock',
             manifest_sha256=audio.digest(tmp_path/'manifest.json'),elf_sha256='a'*64,
             target_dependencies_lock_sha256='c'*64)
    sidecar=tmp_path/'board-provenance.json';sidecar.write_text(json.dumps(row))
    assert audio.target_lock(tmp_path,manifest,'p4xc')=='c'*64
    row['manifest_sha256']='d'*64;sidecar.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='binding'):audio.target_lock(tmp_path,manifest,'p4xc')


def declared_audio():
    return dict(p4_audio=dict(analog_db=30,digital_percent=125,playback_volume_db=-6,
                             playback_seed_dbfs_x10=-289,tx_stack_bytes=28672),
                p4_overlay_values=dict(CONFIG_DG_MIC_GAIN_PERCENT='125'),
                p4_overlay_keydiff=dict(equal=['CONFIG_DG_MIC_GAIN_PERCENT'],differ=[],removed=[]))


class AudioConsole(Console):
    def __init__(self, analog=30, digital=125, volume=-6, register=None):
        super().__init__();self.analog=analog;self.digital=digital;self.volume=volume;self.register=register
    def send(self, command):
        self.sent.append(command);seq=len(self.sent)
        if command=='mic-gain-status':
            reg=(0x10|self.analog//3) if self.register is None else self.register
            self.rows.append(f'MIC_GAIN_PATH analog_db={self.analog} reg=0x{reg:02x} digital_percent={self.digital}')
        if command=='stats':self.rows.append(f'OUTPUT_PATH volume_relative_db={self.volume} accepted_pcm_peak=0')
        self.rows.append(f'OK #{seq} {command}');return seq


def test_boot_audio_readback_checks_mic_and_level_without_changing_them():
    board=AudioConsole();observed=audio.verify_audio(board,declared_audio())
    assert observed==dict(analog_db=30,digital_percent=125,playback_volume_db=-6)
    assert board.sent==['mic-gain-status','stats']


@pytest.mark.parametrize('options',[dict(digital=1600),dict(analog=24),dict(volume=0),dict(register=0x10)])
def test_stale_or_wrong_boot_audio_is_rejected(options):
    with pytest.raises(ContractError):audio.verify_audio(AudioConsole(**options),declared_audio())


@pytest.mark.parametrize('mutation',['missing','overlay','keydiff'])
def test_bundle_requires_audio_declaration_and_overlay_proof(mutation):
    manifest=declared_audio()
    if mutation=='missing':manifest.pop('p4_audio')
    elif mutation=='overlay':manifest['p4_overlay_values']['CONFIG_DG_MIC_GAIN_PERCENT']='1600'
    else:manifest['p4_overlay_keydiff']['differ']=['CONFIG_DG_MIC_GAIN_PERCENT']
    with pytest.raises(ContractError):audio.audio_settings(manifest)


@pytest.fixture
def acoustic_case(tmp_path):
    import math
    import numpy as np
    from speaker_dual_sweep_test import correct
    plan=json.loads(audio.PLAN.read_text())
    calls=[];commands=[]
    options=dict(fail_at=None,nonlinear=False,dump_failure=False,capture_failure=False)
    class Board:
        cursor=0
        def command(self,text):commands.append(text)
        def lines_since(self,at):return []
    def capture(board,device,out,name,hz,amp):
        calls.append((name,hz,amp))
        if options['capture_failure']:raise RuntimeError('tone IO failed')
        offset=.1 if name.startswith('r2-') else 0
        row=dict(name=name,hz=hz,amplitude_percent=amp,chains={})
        for chain,shift in [('board',0),('jabra',5)]:
            level=-60+20*math.log10(amp)+shift+offset
            if options['nonlinear'] and name=='linear-2' and chain=='jabra':level+=3
            bad=options['fail_at']==(name,chain)
            row['chains'][chain]=dict(fundamental_dbfs=level,clipping_count=0,
                harmonics_dbc={f'H{i}':(None if hz*i>=8000 else -10 if bad and i==2 else -40) for i in range(2,6)})
        return row
    def verify(lines,*,prefix):
        assert prefix=='MIC_DUMP'
        if options['dump_failure']:raise ValueError('CHECK mismatch')
    prompt=Mock(side_effect=lambda board,device,directory,source,scale,name: dict(raw_path=str(directory/'raw.wav')))
    prepared=SimpleNamespace(np=np,sleep=Mock(),device='fake-jabra',room=dict(noise_contaminated=False),plan=plan,
        capture=capture,correct=correct,dump_mic=Mock(),verify_dump=Mock(side_effect=verify),record_prompt=prompt,
        pcm=lambda path: np.array([-32768,1000,32767],dtype=float),dbfs=lambda raw: -30.)
    return Board(),prepared,commands,calls,options


@pytest.mark.parametrize('failure',[None,('r1-1000-4','board'),('r2-1000-4','jabra'),('r1-500-2','board')])
def test_acoustic_sequence_clean_prefix_bands_peaks_and_restore(tmp_path,acoustic_case,failure):
    import math
    board,prepared,commands,calls,options=acoustic_case;options['fail_at']=failure
    # Different initial values prove restore comes from the boot readback,
    # rather than another hardcoded copy of the product placeholder.
    initial=dict(analog_db=24,digital_percent=137)
    row=audio.acoustic(board,tmp_path,prepared,initial)
    expected=prepared.plan['amplitudes_percent'] if failure is None else ([] if failure[0]=='r1-500-2' else [2,3])
    assert row['clean_amplitudes_percent']==expected and row['acquisition_passed']
    assert commands[:3]==['audio 0','quiet 1','playback-volume -6']
    assert commands[-2:]==['mic-analog-gain 24','mic-gain 137']
    assert commands[3:-2]==[cmd for gain in prepared.plan['mic_analog_candidates_db']
                           for cmd in (f'mic-analog-gain {gain}','mic-gain 100','mic-record 1','mic-record 0')]
    assert prepared.sleep.call_args_list==[call(1.5)]*3
    assert len(row['points'])==2*len(prepared.plan['frequencies_hz'])*len(prepared.plan['amplitudes_percent'])
    frequencies=prepared.plan['frequencies_hz'];amplitudes=prepared.plan['amplitudes_percent']
    expected_calls=[(f'cal-{hz}',hz,2) for hz in sorted({f*h for f in frequencies for h in range(1,6) if f*h<8000})]
    expected_calls += [(f'linear-{amp}',1000,amp) for amp in (1,2,4,8)]
    expected_calls += [(f'r{repeat}-{hz}-{amp}',hz,amp) for repeat in (1,2) for hz in frequencies for amp in amplitudes]
    assert calls==expected_calls
    assert all(r['peak']==32768 and r['adc_clips']==2 for r in row['microphones'])
    assert len(row['microphones'])==len(prepared.plan['prompts'])*3
    assert prepared.verify_dump.call_count==len(calls)+3+len(row['microphones'])
    if expected:
        assert row['level_band_dbfs']['1000']['board']==pytest.approx([-60+20*math.log10(2),-60+20*math.log10(expected[-1])+.1])
        assert row['level_band_dbfs']['1000']['jabra']==pytest.approx([-55+20*math.log10(2),-55+20*math.log10(expected[-1])+.1])
    else:assert row['level_band_dbfs'] is None
    saved=json.loads((tmp_path/'acoustic.json').read_text());assert saved['gain_restore']['errors']==[]


def test_acoustic_nonlinear_jabra_empties_clean_band(tmp_path,acoustic_case):
    board,prepared,commands,calls,options=acoustic_case;options['nonlinear']=True
    row=audio.acoustic(board,tmp_path,prepared,dict(analog_db=30,digital_percent=125))
    assert row['acquisition_passed'] and row['clean_amplitudes_percent']==[] and row['level_band_dbfs'] is None


@pytest.mark.parametrize('key',['capture_failure','dump_failure'])
def test_acoustic_io_failure_still_restores_gain_and_never_claims_acquisition(tmp_path,acoustic_case,key):
    board,prepared,commands,calls,options=acoustic_case;options[key]=True
    with pytest.raises((RuntimeError,ValueError)):
        audio.acoustic(board,tmp_path,prepared,dict(analog_db=24,digital_percent=137))
    assert commands[-2:]==['mic-analog-gain 24','mic-gain 137']
    assert not json.loads((tmp_path/'acoustic.json').read_text())['acquisition_passed']


def test_quiet_room_preflight_imports_and_baseline_do_not_use_a_board(tmp_path,monkeypatch):
    import alsa
    monkeypatch.setattr(alsa,'resolve_jabra',lambda: ('fake-card','fake-device'))
    mixer=Mock();monkeypatch.setattr(alsa,'set_volumes',mixer)
    monkeypatch.setattr(alsa,'record_room_baseline',lambda *args: dict(noise_contaminated=False))
    prepared=audio.preflight_acoustic(tmp_path)
    assert prepared.device=='fake-device' and callable(prepared.capture)
    mixer.assert_called_once_with('fake-card',tmp_path/'mixer.log')
    monkeypatch.setattr(alsa,'record_room_baseline',lambda *args: dict(noise_contaminated=True))
    with pytest.raises(ContractError,match='quiet-room'):audio.preflight_acoustic(tmp_path)


@pytest.mark.parametrize('bad',[dict(error='cleanup failed'),dict(restore=dict(clean=False)),
                               dict(restore=None),dict(cleanup_errors=['failed']),dict(interrupted=True)])
def test_pair_and_costs_refuse_errored_acquisitions(pair,tmp_path,bad):
    left,right,out=pair
    path=right/'result.json';row=json.loads(path.read_text());row.update(bad);path.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='restore'):audio.product_pair(left,right,out)
    directory=tmp_path/'cost-run';cost_fixture(directory)
    path=directory/'result.json';row=json.loads(path.read_text());row.update(bad);path.write_text(json.dumps(row))
    with pytest.raises(ContractError,match='restore'):audio.session_costs(directory,tmp_path/'costs.json')


def test_sweep_gate_requires_nonempty_band_and_matching_image(tmp_path):
    manifest=dict(elf_sha256='a'*64)
    (tmp_path/'result.json').write_text(json.dumps(dict(acquisition_passed=True,board='p4xc',
        elf_sha256='a'*64,restore=dict(clean=True))))
    path=tmp_path/'acoustic.json';path.write_text(json.dumps(dict(acquisition_passed=True,
        clean_amplitudes_percent=[2,3],level_band_dbfs={'1000':{'jabra':[-50,-40]}})))
    assert audio.sweep_gate(tmp_path,manifest)['clean_amplitudes_percent']==[2,3]
    for mutation in (dict(clean_amplitudes_percent=[]),dict(level_band_dbfs=None),dict(acquisition_passed=False)):
        row=json.loads(path.read_text());row.update(mutation);path.write_text(json.dumps(row))
        with pytest.raises(ContractError):audio.sweep_gate(tmp_path,manifest)


def test_voice_boot_runs_the_same_audio_readback_guard(monkeypatch):
    monkeypatch.setattr(smoke.LiveIO,'boot',lambda self,board: 123)
    io=object.__new__(audio.VoiceIO);io.args=SimpleNamespace(board='p4xc');io.manifest=declared_audio()
    assert io.boot(AudioConsole())==123 and io.boot_audio['digital_percent']==125
    with pytest.raises(ContractError,match='differs'):io.boot(AudioConsole(digital=1600))


@pytest.fixture
def bundle_metadata_case(tmp_path,monkeypatch):
    import sys
    monkeypatch.syspath_prepend(str(audio.ROOT/'scripts'))
    import p4_bundle
    root=tmp_path/'repo';root.mkdir();monkeypatch.setattr(p4_bundle,'ROOT',root)
    for name in ('components/dg_board/test_host/audio_profile.cpp','components/dg_board/src/p4xc/audio_hw.cpp',
                 'components/dg_board/include/dg_board/audio_hw.h'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('// fixture\n')
    overlay=root/'boards/p4xc/sdkconfig.defaults';overlay.parent.mkdir(parents=True)
    overlay.write_text('CONFIG_DG_MIC_GAIN_PERCENT=125\nCONFIG_IDF_TARGET="esp32p4"\n')
    build=root/'build';build.mkdir();config=build/'sdkconfig'
    config.write_text('CONFIG_DG_MIC_GAIN_PERCENT=125\nCONFIG_IDF_TARGET="esp32p4"\n'
        'CONFIG_DG_PLAYBACK_SEED_DBFS_X10=-301\nCONFIG_DG_AUDIO_TX_STACK_BYTES=30720\nCONFIG_PASSWORD="private-test"\n')
    # Resolve the actual Kconfig files, with deliberately different fixture
    # P4 defaults. No second copy of production default values is asserted.
    for name in ('main/Kconfig.projbuild','components/pipecat_esp_peer/Kconfig','components/dg_audio/Kconfig'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        text=(audio.ROOT/name).read_text()
        if name.endswith('pipecat_esp_peer/Kconfig'):
            import re
            text=re.sub(r'default -\d+ if DG_BOARD_P4XC', 'default -301 if DG_BOARD_P4XC',text)
            text=re.sub(r'default \d+ if DG_BOARD_P4XC && !DG_OPUS_UPLINK_FRAME40',
                        'default 30720 if DG_BOARD_P4XC && !DG_OPUS_UPLINK_FRAME40',text)
        path.write_text(text)
    (build/'project_description.json').write_text(json.dumps(dict(target='esp32p4',project_path=str(root),
        config_file=str(config),config_defaults=str(overlay))))
    profile=root/'profile';profile.write_text('host tool fixture')
    monkeypatch.setattr(p4_bundle.subprocess,'check_output',lambda command,**kw: '{"analog_db":24,"playback_volume_db":-9}')
    return p4_bundle,build,profile,config


def test_bundle_metadata_reads_actual_public_config_and_table_without_copying_private_values(bundle_metadata_case):
    module,build,profile,config=bundle_metadata_case
    row=module.metadata(build,profile)
    assert row['p4_audio']==dict(analog_db=24,playback_volume_db=-9,digital_percent=125,
                              playback_seed_dbfs_x10=-301,tx_stack_bytes=30720)
    assert row['p4_overlay_keydiff']['differ']==[]
    assert row['p4_overlay_values']['CONFIG_DG_MIC_GAIN_PERCENT']=='125'
    assert 'private-test' not in json.dumps(row) and 'CONFIG_PASSWORD' not in json.dumps(row)
    config.write_text(config.read_text().replace('GAIN_PERCENT=125','GAIN_PERCENT=1600'))
    with pytest.raises(ValueError,match='overlay differs'):module.metadata(build,profile)


def test_bundle_metadata_refuses_stale_table_emitter(bundle_metadata_case):
    import os
    module,build,profile,config=bundle_metadata_case
    table=module.ROOT/'components/dg_board/src/p4xc/audio_hw.cpp'
    os.utime(table,ns=(profile.stat().st_mtime_ns+100000,profile.stat().st_mtime_ns+100000))
    with pytest.raises(ValueError,match='Rebuild'):module.metadata(build,profile)


def test_voice_gate_precedes_bot_and_device_construction(tmp_path,monkeypatch):
    manifest=dict(profile='normal',aec_enabled=False,elf_sha256='a'*64,**declared_audio())
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (manifest,{}))
    factory=Mock();monkeypatch.setattr(audio,'VoiceIO',factory)
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',acoustic_run=None)
    with pytest.raises(ContractError,match='acoustic-run'):audio.voice_smoke(args)
    factory.assert_not_called()


def test_voice_delegates_to_frozen_execute_and_retains_sweep_evidence(tmp_path,monkeypatch):
    manifest=dict(profile='normal',aec_enabled=False,elf_sha256='a'*64,**declared_audio())
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (manifest,{}))
    monkeypatch.setattr(audio,'sweep_gate',lambda *args: dict(acoustic_sha256='b'*64))
    io=SimpleNamespace(boot_audio=dict(digital_percent=125))
    monkeypatch.setattr(audio,'VoiceIO',lambda *args: io)
    def execute(args,*,backend):
        assert backend is io and args.headless and args.calibration
        args.out.mkdir()
        return dict(acquisition_passed=True,restore=dict(clean=True))
    monkeypatch.setattr(smoke,'execute',execute)
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',acoustic_run=tmp_path/'sweep',
        out=tmp_path/'voice',bot_identity=identity,headless=True,calibration=True)
    assert audio.voice_smoke(args)==0
    result=json.loads((args.out/'result.json').read_text())
    assert result['acoustic_sweep']['acoustic_sha256']=='b'*64
    assert result['boot_audio']['digital_percent']==125


@pytest.mark.parametrize('key,old,new',[
    ('CONFIG_DG_PLAYBACK_SEED_DBFS_X10','-301','-302'),
    ('CONFIG_DG_AUDIO_TX_STACK_BYTES','30720','31744')])
def test_changed_kconfig_default_rejects_stale_config_before_bundle_creation(bundle_metadata_case,monkeypatch,key,old,new):
    module,build,profile,config=bundle_metadata_case
    assert module.metadata(build,profile)['p4_kconfig_defaults'][key]==old
    peer=module.ROOT/'components/pipecat_esp_peer/Kconfig'
    peer.write_text(peer.read_text().replace(f'default {old} if DG_BOARD_P4XC',f'default {new} if DG_BOARD_P4XC'))
    create=Mock();monkeypatch.setattr(module,'make_bundle',create)
    out=build/'bundle'
    with pytest.raises(ValueError,match=key):module.bundle(build,out,profile)
    create.assert_not_called();assert not out.exists()
    # Regenerating just the fixture's resolved public value clears the guard.
    config.write_text(config.read_text().replace(f'{key}={old}',f'{key}={new}'))
    assert module.metadata(build,profile)['p4_kconfig_defaults'][key]==new


@pytest.mark.parametrize('flag',['DG_ECHOEAR_AEC','DG_OPUS_UPLINK_FRAME40'])
def test_kconfig_resolution_uses_current_variant_defaults(bundle_metadata_case,flag):
    module,build,profile,config=bundle_metadata_case
    project=json.loads((build/'project_description.json').read_text())
    overlay=Path(project['config_defaults']);overlay.write_text(overlay.read_text()+f'CONFIG_{flag}=y\n')
    config.write_text(config.read_text()+f'CONFIG_{flag}=y\n')
    with pytest.raises(ValueError,match='CONFIG_DG_AUDIO_TX_STACK_BYTES'):module.metadata(build,profile)


class DelayedAudioConsole(AudioConsole):
    def __init__(self,deliver=True):super().__init__();self.deliver=deliver;self.pending=None;self.waits=[]
    def send(self,command):
        seq=super().send(command)
        self.pending=self.rows.pop(-2)  # Hold telemetry until after its ACK.
        return seq
    def wait_for(self,pattern,timeout,since):
        self.waits.append((pattern,timeout,since))
        if 'MIC_GAIN_PATH' in pattern or 'OUTPUT_PATH' in pattern:
            if not self.deliver:raise TimeoutError('delayed record missing')
            self.rows.append(self.pending);return self.pending
        return self.rows[-1]


def test_boot_readback_waits_for_late_records_without_repeating_commands():
    board=DelayedAudioConsole()
    assert audio.verify_audio(board,declared_audio())['digital_percent']==125
    assert board.sent==['mic-gain-status','stats']
    assert [timeout for pattern,timeout,since in board.waits if 'PATH' in pattern]==[2,2]


def test_boot_readback_missing_record_exhausts_only_the_brief_record_wait():
    board=DelayedAudioConsole(deliver=False)
    with pytest.raises(TimeoutError):audio.verify_audio(board,declared_audio())
    assert board.sent==['mic-gain-status'] and board.waits[-1][1]==2


@pytest.fixture
def acoustic_run(tmp_path,acoustic_case):
    board,prepared,commands,calls,options=acoustic_case
    options['fail_at']=('r1-500-2','board')
    directory=tmp_path/'original';directory.mkdir()
    row=audio.acoustic(board,directory,prepared,dict(analog_db=30,digital_percent=125))
    assert row['clean_amplitudes_percent']==[]
    (directory/'result.json').write_text(json.dumps(dict(acquisition_passed=True,restore=dict(clean=True))))
    return directory


def test_reanalysis_is_labelled_and_preserves_every_original_byte(tmp_path,acoustic_run):
    before={p:p.read_bytes() for p in acoustic_run.glob('*.json')}
    out=tmp_path/'alternative.json';row=audio.reanalyze(acoustic_run,[500],out)
    assert row['excluded_hz']==[500] and 500 not in row['retained_hz']
    assert row['source_clean_amplitudes_percent']==[] and row['clean_amplitudes_percent']==[2,3,4,6,8,10,15,20,26,30]
    assert not row['qualification'] and not row['decision_applied']
    assert set(row['level_band_dbfs'])=={'800','1000','1500','3000'}
    assert all(p.read_bytes()==content for p,content in before.items())
    with pytest.raises(ContractError,match='new reanalysis'):audio.reanalyze(acoustic_run,[500],out)
    assert json.loads(out.read_text())==row


@pytest.mark.parametrize('excluded',[[],[999],[500,800,1000,1500,3000]])
def test_reanalysis_rejects_unknown_empty_or_total_exclusions(tmp_path,acoustic_run,excluded):
    out=tmp_path/'bad.json'
    with pytest.raises(ContractError):audio.reanalyze(acoustic_run,excluded,out)
    assert not out.exists()


def test_reanalysis_recomputes_linearity_and_does_not_bypass_failure(tmp_path,acoustic_run):
    path=acoustic_run/'acoustic.json';row=json.loads(path.read_text())
    row['linearity'][1]['chains']['jabra']['fundamental_dbfs']+=3
    # Cached passing summary is intentionally left intact.
    path.write_text(json.dumps(row))
    result=audio.reanalyze(acoustic_run,[500],tmp_path/'nonlinear.json')
    assert result['clean_amplitudes_percent']==[] and result['level_band_dbfs'] is None



def test_reanalysis_cannot_write_into_read_only_acquisition(acoustic_run):
    with pytest.raises(ContractError,match='outside'):
        audio.reanalyze(acoustic_run,[500],acoustic_run/'new-result.json')
    assert not (acoustic_run/'new-result.json').exists()


def measured_chain(snr=20,h2=-30,h3=-30,thd=1,clips=0):
    return dict(fundamental_dbfs=-70+snr,noise_dbfs=-70,corrected_thd_percent=thd,
                corrected_harmonics_dbc=dict(H2=h2,H3=h3),clipping_count=clips)


@pytest.mark.parametrize('snr,state',[(9.999,'unmeasurable'),(10,'pass'),(10.001,'pass')])
def test_decision_snr_boundary_is_strict(snr,state):
    assert audio.classify_chain(measured_chain(snr),10)['classification']==state


@pytest.mark.parametrize('field,value',[('h2',-25.999),('h3',-25.999),('thd',5.001),('clips',1)])
def test_measurable_chain_keeps_original_distortion_and_clip_limits(field,value):
    chain=measured_chain(**{field:value})
    assert audio.classify_chain(chain,10)['classification']=='fail'
    chain['fundamental_dbfs']=-60.001
    assert audio.classify_chain(chain,10)['classification']=='unmeasurable'


def test_exact_distortion_limits_and_missing_above_nyquist_h3_pass():
    assert audio.classify_chain(measured_chain(h2=-26,h3=-26,thd=5),10)['classification']=='pass'
    assert audio.classify_chain(measured_chain(h3=None),10)['classification']=='pass'


def decision_points():
    rows=[]
    for amp in (2,4,6,8,10,15):
        for hz in (500,800,1000):
            for repeat in (1,2):
                chain=measured_chain(9 if amp==2 or amp==4 and hz==1000 else 20)
                if amp==8 and hz==1000 and repeat==1:chain=measured_chain(9)
                if hz==500:chain=measured_chain(h2=-20)
                jabra=dict(chain)
                if amp==10 and hz==800 and repeat==2:jabra=measured_chain(h2=-25)
                rows.append(dict(name=f'r{repeat}-{hz}-{amp}',hz=hz,amplitude_percent=amp,repeat=repeat,
                                 chains=dict(board=chain,jabra=jabra)))
    return rows


def test_decision_band_start_requires_a_measurable_pass_at_every_frequency_and_never_restarts():
    row=audio.snr_band(decision_points(),dict(board=[6.0206]*3,jabra=[6.0206]*3),[800,1000],[2,4,6,8,10,15],10)
    assert row['clean_amplitudes_percent']==[6,8]
    assert all(p['classification']=='unmeasurable' for p in row['points'] if p['hz']!=500 and p['amplitude_percent']==2)
    assert all(p['excluded'] for p in row['points'] if p['hz']==500)
    low=[p for p in row['points'] if p['hz']==1000 and p['amplitude_percent']==8 and p['repeat']==1][0]
    assert low['classification']=='unmeasurable'
    # Unmeasurable chain levels do not establish the measured dBFS band.
    assert row['level_band_dbfs']['1000']['board']==[-50,-50]


def test_both_chains_unmeasurable_never_establish_a_band_start():
    points=decision_points()
    for row in points:
        row['chains']={name:measured_chain(9) for name in ('board','jabra')}
    result=audio.snr_band(points,dict(board=[6.0206]*3,jabra=[6.0206]*3),[800,1000],[2,4,6,8,10,15],10)
    assert result['clean_amplitudes_percent']==[] and result['level_band_dbfs'] is None


def test_preband_measurable_failure_prevents_that_amplitude_from_starting_the_band():
    points=decision_points()
    for row in points:
        if row['amplitude_percent']==6 and row['hz']==1000:
            row['chains']['board']=measured_chain(h2=-20)
    result=audio.snr_band(points,dict(board=[6.0206]*3,jabra=[6.0206]*3),[800,1000],[2,4,6,8,10,15],10)
    assert result['clean_amplitudes_percent']==[8]


@pytest.fixture
def decision_analysis(tmp_path):
    directory=tmp_path/'original';directory.mkdir()
    data=dict(acquisition_passed=True,plan_sha256='c'*64,clean_amplitudes_percent=[],level_band_dbfs=None,
              points=decision_points(),linearity=[dict(chains={c:dict(fundamental_dbfs=level) for c in ('board','jabra')})
                                                for level in (-70,-63.9794,-57.9588,-51.9382)])
    (directory/'acoustic.json').write_text(json.dumps(data))
    (directory/'result.json').write_text(json.dumps(dict(board='p4xc',elf_sha256='a'*64,
        acquisition_passed=True,restore=dict(clean=True,action='keep-tested-p4'))))
    out=tmp_path/'decision.json'
    row=audio.reanalyze(directory,[500],out,10)
    return directory,out,row


def test_decision_reanalysis_records_rule_threshold_classification_hashes_and_eligible_band(decision_analysis):
    directory,out,row=decision_analysis
    assert row['decision_id']==audio.DECISION_ID and row['decision_applied'] and row['post_hoc']
    assert not row['qualification'] and row['rule']['min_chain_snr_db']==10 and row['points']
    result=audio.reanalysis_gate(out,dict(elf_sha256='a'*64))
    assert result['decision_id']==audio.DECISION_ID and result['clean_amplitudes_percent']==[6,8]
    with pytest.raises(ContractError,match='non-empty'):audio.sweep_gate(directory,dict(elf_sha256='a'*64))


@pytest.mark.parametrize('source',['acoustic.json','result.json'])
def test_reanalysis_gate_refuses_input_hash_mismatch_before_voice(decision_analysis,source):
    directory,out,row=decision_analysis
    path=directory/source;path.write_text(path.read_text()+'\n')
    with pytest.raises(ContractError,match='input hash mismatch'):audio.reanalysis_gate(out,dict(elf_sha256='a'*64))


@pytest.mark.parametrize('mutation',['elf','decision','threshold','band','restore'])
def test_reanalysis_gate_requires_current_elf_approved_rule_clean_restore_and_authentic_band(decision_analysis,mutation):
    directory,out,row=decision_analysis
    manifest=dict(elf_sha256='a'*64)
    if mutation=='elf':manifest['elf_sha256']='b'*64
    elif mutation=='decision':row['decision_id']='unapproved'
    elif mutation=='threshold':row['rule']['min_chain_snr_db']=11
    elif mutation=='band':row['clean_amplitudes_percent']=[2,4,6,8,10,15]
    else:
        source=directory/'result.json';run=json.loads(source.read_text());run['restore']['clean']=False
        source.write_text(json.dumps(run));row['source_result_sha256']=audio.digest(source)
    out.write_text(json.dumps(row))
    with pytest.raises(ContractError):audio.reanalysis_gate(out,manifest)


def test_voice_smoke_accepts_reanalysis_eligibility_and_records_decision(tmp_path,decision_analysis,monkeypatch):
    directory,out,row=decision_analysis
    manifest=dict(profile='normal',aec_enabled=False,elf_sha256='a'*64,**declared_audio())
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (manifest,{}))
    io=SimpleNamespace(boot_audio=dict(digital_percent=125));monkeypatch.setattr(audio,'VoiceIO',lambda *args: io)
    def execute(args,*,backend):
        args.out.mkdir();return dict(acquisition_passed=True,restore=dict(clean=True))
    monkeypatch.setattr(smoke,'execute',execute)
    identity=tmp_path/'identity.json';identity.write_text(json.dumps(IDENTITY))
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',acoustic_run=None,acoustic_reanalysis=out,
                         out=tmp_path/'voice',bot_identity=identity)
    assert audio.voice_smoke(args)==0
    result=json.loads((args.out/'result.json').read_text())
    assert result['acoustic_sweep']['decision_id']==audio.DECISION_ID
    assert result['acoustic_sweep']['reanalysis_sha256']==audio.digest(out)


def test_one_measurable_chain_pass_can_establish_coverage_with_other_chain_unmeasurable():
    points=[dict(name=f'r{repeat}-800-2',hz=800,amplitude_percent=2,repeat=repeat,
                 chains=dict(board=measured_chain(9),jabra=measured_chain(10))) for repeat in (1,2)]
    result=audio.snr_band(points,dict(board=[6.0206]*3,jabra=[6.0206]*3),[800],[2],10)
    assert result['clean_amplitudes_percent']==[2]
    assert all(p['classification']=='pass' for p in result['points'])
    assert result['level_band_dbfs']['800']['board'] is None
    assert result['level_band_dbfs']['800']['jabra']==[-60,-60]


def test_voice_reanalysis_hash_mismatch_refuses_before_live_backend_creation(tmp_path,decision_analysis,monkeypatch):
    directory,out,row=decision_analysis
    manifest=dict(profile='normal',aec_enabled=False,elf_sha256='a'*64,**declared_audio())
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (manifest,{}))
    factory=Mock();monkeypatch.setattr(audio,'VoiceIO',factory)
    path=directory/'acoustic.json';path.write_text(path.read_text()+'\n')
    args=SimpleNamespace(board='p4xc',bundle=tmp_path/'bundle',acoustic_run=None,acoustic_reanalysis=out)
    with pytest.raises(ContractError,match='hash mismatch'):audio.voice_smoke(args)
    factory.assert_not_called()


def test_reanalysis_decision_cannot_be_applied_to_s3_voice_smoke(tmp_path,monkeypatch):
    monkeypatch.setattr(audio,'verify_bundle',lambda *args: (dict(profile='normal',aec_enabled=False),{}))
    args=SimpleNamespace(board='echoear',bundle=tmp_path/'bundle',acoustic_reanalysis=tmp_path/'decision.json')
    with pytest.raises(ContractError,match='P4 only'):audio.voice_smoke(args)
