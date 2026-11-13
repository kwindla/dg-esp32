"""Step 14 adapters: offline controls; no build, server, serial or audio IO."""
import io
import json
from pathlib import Path
import re
import shlex
import importlib.util
import shutil
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import board_smoke as smoke
import p4_product
import p4_step14 as step
from make_bundle import digest
from p4_liveness_bot import install_tap, packet
from test_board_smoke import events
from test_p4_product import bundle
from test_p4_contention_live import rig


@pytest.mark.parametrize('kind,aec,city',[('france',False,'Paris'),('talkover',True,'Rome')])
def test_negated_city_requires_qualification_but_is_report_only_in_calibration(kind,aec,city):
    data=events(kind,aec)
    for row in data:
        if row.get('kind')=='aggregator-assistant-turn-stopped':row['text']='Definitely not '+city
    with pytest.raises(Exception,match='negates'):step.turn_checks(data,kind,aec)
    result=step.turn_checks(data,kind,aec,calibration=True)
    assert result['passed'] and any(r['kind']=='off_script_reply' for r in result['bot_content_findings'])


def test_contention_top_level_unmeasured_does_not_claim_measured_overflow_pass():
    report=dict(checks_passed=True,board='echoear',generations=[dict(resources=dict(capture_overflows_result='unmeasured (report-only)'))])
    result=step.contention_summary(report)
    assert result['checks_passed'] and not result['all_checks_measured']
    assert result['capture_overflows_summary']=='unmeasured (report-only)'
    assert 'all_checks_measured' not in report
    pair=dict(capture_overflow_evidence=[dict(generations=[dict(capture_overflows_result='measured')]),
                                       dict(generations=[dict(capture_overflows_result='unmeasured (report-only)')])])
    assert not step.contention_summary(pair)['all_checks_measured']


def pinned(tmp_path):
    root=bundle(tmp_path);path=root/'manifest.json';manifest=json.loads(path.read_text())
    manifest.update(target_dependencies_lock='dependencies.p4xc.lock',
        target_dependencies_lock_sha256='a'*64,dependencies_lock_sha256='a'*64)
    path.write_text(json.dumps(manifest))
    headroom=tmp_path/'headroom.json';headroom.write_text(json.dumps(dict(passed=True,elf_sha256=manifest['elf_sha256'])))
    record=tmp_path/'restore.json';step.pin(root,headroom,record)
    return root,record


def test_pin_is_normal_only_and_reauthenticates_all_inputs(tmp_path):
    root,path=pinned(tmp_path);expected=digest(path)
    result=step.restore_record(path,expected)
    assert result['storage']=='erase named storage before boot' and not result['boot_verified']
    with pytest.raises(Exception,match='hash mismatch'):step.restore_record(path,'0'*64)
    (root/'dg-esp32.bin').write_bytes(b'changed')
    with pytest.raises(Exception,match='hash mismatch'):step.restore_record(path,expected)


@pytest.mark.parametrize('field,value',[('autostart',True),('aec_enabled',True),('dependencies_lock_sha256','b'*64)])
def test_restore_pin_refuses_demo_aec_or_wrong_target_lock(tmp_path,field,value):
    root,path=pinned(tmp_path);manifest=root/'manifest.json';data=json.loads(manifest.read_text())
    data[field]=value;manifest.write_text(json.dumps(data))
    with pytest.raises(Exception):step.pin(root,tmp_path/'headroom.json',tmp_path/'refused.json')
    assert not (tmp_path/'refused.json').exists()


def test_restore_flash_erases_before_return_to_serial_and_restores_hooks(tmp_path,monkeypatch):
    root,path=pinned(tmp_path);calls=[]
    monkeypatch.setattr(smoke.LiveIO,'preflight_restore',lambda io:calls.append(('preflight',io.args.p4_restore_elf)))
    monkeypatch.setattr(smoke.LiveIO,'flash',lambda io,*args:calls.append(('flash',args[-1])))
    monkeypatch.setattr(p4_product,'prepare_storage',lambda bundle,out:calls.append(('erase',out.name)))
    original=smoke.LiveIO.flash
    device=SimpleNamespace(args=SimpleNamespace(board='p4xc'),out=tmp_path)
    with step.restoring(path,digest(path)):
        smoke.LiveIO.preflight_restore(device)
        smoke.LiveIO.flash(device,root,json.loads((root/'manifest.json').read_text()),'restore')
        smoke.LiveIO.flash(device,root,json.loads((root/'manifest.json').read_text()),'restore')
    assert calls[-4:]==[('flash','restore'),('erase','restore-storage-001'),('flash','restore'),('erase','restore-storage-002')]
    assert smoke.LiveIO.flash is original


def test_p4_missing_pin_fails_before_flash_and_s3_delegates(monkeypatch):
    calls=[];monkeypatch.setattr(smoke.LiveIO,'preflight_restore',lambda io:calls.append(io.args.board))
    with step.restoring(None,None):
        with pytest.raises(Exception,match='require'):smoke.LiveIO.preflight_restore(SimpleNamespace(args=SimpleNamespace(board='p4xc')))
        smoke.LiveIO.preflight_restore(SimpleNamespace(args=SimpleNamespace(board='echoear')))
    assert calls==['echoear']


@pytest.mark.parametrize('board,label',[('p4xc','candidate'),('echoear','restore')])
def test_factory_sentinel_applies_only_to_p4_restore(tmp_path,monkeypatch,board,label):
    root,path=pinned(tmp_path);calls=[]
    (tmp_path/'select-factory').touch()
    args=SimpleNamespace(board=board,p4_restore_bundle=root,p4_restore_elf='preserved')
    device=SimpleNamespace(args=args,out=tmp_path,guard=SimpleNamespace(success=True))
    monkeypatch.setattr(smoke.LiveIO,'flash',lambda *args:calls.append('flash'))
    monkeypatch.setattr(p4_product,'prepare_storage',lambda *args:pytest.fail('unexpected erase'))
    with step.restoring(path,digest(path)):
        smoke.LiveIO.flash(device,root,json.loads((root/'manifest.json').read_text()),label)
    assert calls==['flash'] and device.guard.success
    assert args.p4_restore_bundle==root and args.p4_restore_elf=='preserved'


@pytest.mark.parametrize('mode',['manual','demo'])
@pytest.mark.parametrize('storage',['blank','ready','unknown'])
@pytest.mark.parametrize('fixed',[False,True])
def test_blank_p4_provisions_then_loads_with_preloaded_fixture_origin(tmp_path,monkeypatch,mode,storage,fixed):
    """Real smoke/demo/fixture flow; fake only console, HTTP server and thread.

    Match preflight's early origin import followed by smoke's late 360px pack.
    The old path already formats blank storage, then fails fetch against the
    wrong directory. The adapter must align sources without another format.
    """
    import step9a_assets as assets,step9a_support as display
    root,path=pinned(tmp_path);fixture_root=Path(__file__).resolve().parent/'fixtures'
    monkeypatch.setattr(assets,'ROOT',fixture_root/'step9a')
    monkeypatch.setattr(display,'FIXTURES',fixture_root/'echoear360')
    monkeypatch.setenv('STEP9A_FIXTURES',str(fixture_root/'echoear360'))
    monkeypatch.setattr(display,'host_address',lambda:'127.0.0.1')
    monkeypatch.setattr(display,'BUDGET_PATH',tmp_path/'unused-budget.json')
    before=(assets.ROOT,display.FIXTURES,__import__('os').environ['STEP9A_FIXTURES'])
    state=SimpleNamespace(origin=None,commands=[],provisions=0,formats=0,loads=0,healthy=storage=='ready',inventory={})
    class Server:
        def __init__(self,*args):state.origin=assets.ROOT
        def serve_forever(self):pytest.fail('fake server thread must never run')
        def shutdown(self):pass
        def server_close(self):pass
    class Thread:
        def __init__(self,*args,**kwargs):pass
        def start(self):pass
        def join(self,**kwargs):pass
    monkeypatch.setattr(assets,'ThreadingHTTPServer',Server)
    monkeypatch.setattr(assets,'threading',SimpleNamespace(Thread=Thread))
    class Board:
        def __init__(self):self.lines=[]
        @property
        def cursor(self):return len(self.lines)
        def emit(self,prefix,data):self.lines.append((self.cursor,0,prefix+' '+json.dumps(data,separators=(',',':'))))
        def lines_since(self,at):return self.lines[at:]
        def wait_for(self,pattern,*args,since=0):
            return next((line for _,_,line in self.lines_since(since) if re.search(pattern,line)),None) or pytest.fail(pattern)
        def command(self,text,*unused):
            state.commands.append(text)
            if text=='stats':
                self.emit('PERF',dict(display=dict(lcd_initialized=True),heap=dict(psram=dict(free=25000000,largest=24000000))))
            elif text=='disp status':
                self.emit('DISPLAY',dict(flash=dict(health='ready' if state.healthy else 'unavailable'),ram=dict(budget=0)))
            elif text=='disp provision-empty':
                state.provisions+=1
                if storage!='blank':raise RuntimeError('in-use: unknown storage must not be formatted')
                state.formats+=1;state.healthy=True
            elif text.startswith(('disp load ','disp persist ')):
                kind,body=text.split(' ',2)[1:];body=json.loads(body);rid=body['id']
                self.emit('DISPLAY_POST',dict(id=rid))
                error='none'
                if kind=='load':
                    state.loads+=1;source=state.origin/(body['hash']+'.dgi')
                    if not state.healthy or not source.is_file():error='fetch'
                    else:
                        assert digest(source)==body['hash'] and source.stat().st_size==body['bytes']
                        state.inventory[body['hash']]=body['bytes']
                self.emit('DISPLAY_WORK_RESULT',dict(id=rid,error=error,persisted=error=='none'))
            elif text.startswith('disp inventory '):
                self.emit('DISPLAY_INVENTORY',dict(items=[[h,n,0,0,True,True,'none'] for h,n in state.inventory.items()],next=0))
    monkeypatch.setattr(smoke.LiveIO,'preflight_restore',lambda *args:None)
    io=object.__new__(smoke.LiveIO)
    io.args=SimpleNamespace(board='p4xc',headless=False,p4_restore_bundle=root,p4_restore_elf='old')
    io.out=tmp_path/'run';io.out.mkdir();board=Board()
    def run():
        io.preflight_restore()
        return io.prepare_manual(board) if mode=='manual' else io.provision(board)
    with step.restoring(path,digest(path)) if fixed else __import__('contextlib').nullcontext():
        if storage=='unknown':
            with pytest.raises(RuntimeError,match='unknown storage'):run()
        elif not fixed:
            with pytest.raises(RuntimeError,match='Fixture load failed: fetch'):run()
        else:
            result=run()
            assert state.loads>=16
            if mode=='demo':assert len(result['persisted_hashes'])>=16
            assert state.origin==fixture_root/'echoear360'
    assert state.provisions==int(storage!='ready') and state.formats==int(storage=='blank')
    if storage=='unknown':assert state.loads==0
    elif not fixed:assert state.healthy and state.loads==1
    if fixed:assert (assets.ROOT,display.FIXTURES,__import__('os').environ['STEP9A_FIXTURES'])==before


@pytest.mark.parametrize('board',['p4xc','echoear'])
@pytest.mark.parametrize('failure',[False,True])
def test_fixture_preflight_scope_restores_cached_roots_and_unset_environment(tmp_path,monkeypatch,board,failure):
    import os,step9a_assets as assets,step9a_support as display
    root,path=pinned(tmp_path);before=(assets.ROOT,display.FIXTURES)
    monkeypatch.delenv('STEP9A_FIXTURES',raising=False)
    def preflight(io):
        if board=='p4xc':
            assert assets.ROOT==display.FIXTURES==Path(__file__).resolve().parent/'fixtures/echoear360'
        else:assert (assets.ROOT,display.FIXTURES)==before and 'STEP9A_FIXTURES' not in os.environ
        if failure:raise RuntimeError('preflight failed')
    monkeypatch.setattr(smoke.LiveIO,'preflight_restore',preflight)
    with step.restoring(path,digest(path)):
        io=SimpleNamespace(args=SimpleNamespace(board=board))
        if failure:
            with pytest.raises(RuntimeError,match='preflight failed'):smoke.LiveIO.preflight_restore(io)
        else:smoke.LiveIO.preflight_restore(io)
    assert (assets.ROOT,display.FIXTURES)==before and 'STEP9A_FIXTURES' not in os.environ


def test_tap_observes_raw_duplicate_pong_and_injects_only_stale_epoch_fill():
    class Channel:
        def __init__(self):self.sent=[];self.received=[]
        def send(self,data):self.sent.append(data)
        def emit(self,event,data):self.received.append((event,data))
    def client(kind,body):return json.dumps(dict(label='rtvi-ai',type='client-message',data=dict(t=kind,d=body)))
    stream=io.StringIO();original=Channel.send;clean=install_tap(Channel,stream,stale_probes=True,clock=lambda:1)
    try:
        first=Channel();first.emit('message',client('display-ready',dict(epoch=11)))
        first.emit('message',client('pong',dict(seq=1)));first.emit('message',client('pong',dict(seq=1)))
        second=Channel();second.emit('message',client('display-ready',dict(epoch=12)))
        probe=json.loads(second.sent[0]);assert probe['data']['d']['epoch']==11
        assert probe['data']['t']=='display.fill'
        assert [r['seq'] for r in map(json.loads,stream.getvalue().splitlines()) if r['kind']=='pong']==[1,1]
        assert packet(json.dumps(dict(label='rtvi-ai',type='secret',data=dict(token='PRIVATE')))) is None
        assert 'PRIVATE' not in stream.getvalue()
    finally:clean()
    assert Channel.send is original


def test_contention_wrong_perf_pc_id_is_rejected(tmp_path,monkeypatch):
    import p4_contention as contention
    from test_p4_contention import valid_run
    acquisition=valid_run(tmp_path,monkeypatch)
    acquisition['sessions'][0]['samples'][-1]['pc_id']='other-peer'
    with pytest.raises(Exception,match='bot session differs'):
        contention.score(tmp_path,tmp_path/'bounds.json',acquisition=acquisition)


def test_contention_wrong_worker_epoch_is_rejected(rig):
    # Reuse the full frozen fake live rig, then corrupt only the durable worker
    # record; a correct bridge acknowledgement cannot excuse the wrong epoch.
    import p4_contention as contention
    args,state,_=rig;original=state.board.emit
    def emit(line):
        if line.startswith('DISPLAY_WORK_RESULT '):
            row=json.loads(line.split(' ',1)[1]);row['epoch']+=1
            line='DISPLAY_WORK_RESULT '+json.dumps(row,separators=(',',':'))
        return original(line)
    state.board.emit=emit
    assert contention.live(args)==1
    result=json.loads((args.out/'acquisition.json').read_text())
    assert 'display worker epoch differs' in result['error']
    assert not result['acquisition_passed'] and state.released


def test_new_live_launches_use_the_checked_server_environment():
    from test_bench_launch_docs import validate
    found=0
    path=Path(__file__).with_name('p4_liveness.md')
    for block in re.findall(r'```bash\n(.*?)```',path.read_text(),re.S):
        for line in re.sub(r'\\\n\s*',' ',block).splitlines():
            if 'hil/bench.py' not in line:continue
            tokens=shlex.split(line.split('hil/bench.py',1)[1])
            validate(tokens[:tokens.index('python')]);found+=1
    assert found==9


def test_s3_matrix_packages_each_variant_before_its_directory_is_reused(tmp_path,monkeypatch):
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('step14_matrix_test',scripts/'step14_s3.py')
    matrix=importlib.util.module_from_spec(spec);spec.loader.exec_module(matrix)
    previous=tmp_path/'previous.json';references=tmp_path/'references.json';refs={}
    previous.write_text(json.dumps(dict(complete=True,variants={v:dict(canonical_config_sha256='same') for v in matrix.baseline.VARIANTS})))
    for v in matrix.baseline.VARIANTS:
        path=tmp_path/('sdkconfig-'+v);path.write_text('CONFIG_FIXTURE=y\n')
        refs[v]=dict(reference=str(path),sha256=digest(path))
    references.write_text(json.dumps(refs));calls=[]
    monkeypatch.setenv('DG_BUILD_LOCK_HELD','1')
    monkeypatch.setattr(matrix.baseline,'snapshot',lambda *args:dict(canonical_config_sha256='same'))
    monkeypatch.setattr(matrix.baseline,'run',lambda command,*args:calls.append(('keydiff',command[2])))
    monkeypatch.setattr(matrix.subprocess,'run',lambda *args,**kwargs:None)
    monkeypatch.setattr(matrix,'make_bundle',lambda build,out,board:calls.append(('bundle',out.name)))
    def main(_):
        for variant in matrix.baseline.VARIANTS:
            calls.append(('build',variant))
            matrix.baseline.snapshot(tmp_path/('build-ws146' if variant=='waveshare_146' else 'shared-build'),
                tmp_path/('sdkconfig-'+variant),tmp_path,tmp_path/'sdkconfig-normal',variant.startswith('aec-'),variant.endswith('demo'))
    monkeypatch.setattr(matrix.baseline,'main',main)
    matrix.main(['--previous',str(previous),'--references',str(references),'--out',str(tmp_path)])
    assert [call for call in calls if call[0]!='keydiff']==[
        ('build','normal'),('bundle','bundle-echoear-normal'),('build','demo'),('bundle','bundle-echoear-demo'),
        ('build','aec-normal'),('bundle','bundle-echoear-aec-normal'),('build','aec-demo'),('bundle','bundle-echoear-aec-demo'),
        ('build','waveshare_146')]
    refs['normal']['sha256']='0'*64;references.write_text(json.dumps(refs));calls.clear()
    with pytest.raises(Exception,match='reference hash changed'):
        matrix.main(['--previous',str(previous),'--references',str(references),'--out',str(tmp_path)])
    assert not calls


def test_audio_default_resolution_never_passes_private_local_defaults(tmp_path,monkeypatch):
    scripts=Path(__file__).resolve().parents[1]/'scripts';monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('step14_bundle_test',scripts/'p4_bundle.py')
    bundle_tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundle_tool)
    monkeypatch.setenv('TMPDIR',str(tmp_path));commands=[]
    def run(command,**kwargs):
        commands.append(command)
        path=Path(command[command.index('--config')+1])
        path.write_text('CONFIG_DG_PLAYBACK_SEED_DBFS_X10=-180\nCONFIG_DG_AUDIO_TX_STACK_BYTES=32768\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bundle_tool.subprocess,'run',run)
    result=bundle_tool.resolve_audio_defaults(dict(config_defaults='boards/common/sdkconfig.defaults;sdkconfig.local;boards/p4xc/sdkconfig.defaults'))
    assert set(result)=={'CONFIG_DG_PLAYBACK_SEED_DBFS_X10','CONFIG_DG_AUDIO_TX_STACK_BYTES'}
    assert len(commands)==1 and not any(Path(arg).name=='sdkconfig.local' for arg in commands[0])


@pytest.mark.parametrize('case,reason',[('manifest','pin mismatch'),('elf','pin mismatch'),('lock','target lock changed'),
    ('headroom','headroom evidence changed'),('partition','partition table changed'),('storage','invalid P4 restore record')])
def test_pin_reauthenticates_each_record_and_bundle_input(tmp_path,case,reason):
    root,path=pinned(tmp_path);record=json.loads(path.read_text());manifest=root/'manifest.json'
    if case=='manifest':
        data=json.loads(manifest.read_text());data['added_metadata']='different';manifest.write_text(json.dumps(data))
    elif case=='elf':record['elf_sha256']='b'*64
    elif case=='lock':record['target_dependencies_lock_sha256']='b'*64
    elif case=='headroom':Path(record['headroom']).write_text('{}')
    elif case=='storage':record['storage']='preserve'
    else:
        from test_p4_product import table
        table_path=root/'partition_table/partition-table.bin'
        table_path.write_bytes(table(extra=(('extra',1,0,0x900000,0x1000),)))
        data=json.loads(manifest.read_text());data['sha256']['partition_table/partition-table.bin']=digest(table_path)
        manifest.write_text(json.dumps(data));record['manifest_sha256']=digest(manifest)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError,match=reason):step.restore_record(path,digest(path))


@pytest.mark.parametrize('case,reason',[('failed','headroom gate'),('wrong-elf','identify the pinned ELF')])
def test_pin_rejects_unpassed_or_unbound_headroom(tmp_path,case,reason):
    root,path=pinned(tmp_path);headroom=tmp_path/'headroom.json';data=json.loads(headroom.read_text())
    if case=='failed':data['passed']=False
    else:data['elf_sha256']='b'*64
    headroom.write_text(json.dumps(data))
    with pytest.raises(ValueError,match=reason):step.pin(root,headroom,tmp_path/'new-pin.json')
    assert not (tmp_path/'new-pin.json').exists()


def test_preflight_reauthenticates_after_context_entry(tmp_path,monkeypatch):
    root,path=pinned(tmp_path);called=[]
    monkeypatch.setattr(smoke.LiveIO,'preflight_restore',lambda io:called.append('inherited'))
    with step.restoring(path,digest(path)):
        manifest=root/'manifest.json';data=json.loads(manifest.read_text());data['different']=True;manifest.write_text(json.dumps(data))
        with pytest.raises(ValueError,match='pin mismatch'):
            smoke.LiveIO.preflight_restore(SimpleNamespace(args=SimpleNamespace(board='p4xc')))
    assert not called


@pytest.mark.parametrize('case',['path','elf','reauth'])
def test_restore_flash_refuses_other_bundle_elf_or_changed_record(tmp_path,monkeypatch,case):
    root,path=pinned(tmp_path);called=[];manifest=json.loads((root/'manifest.json').read_text())
    monkeypatch.setattr(smoke.LiveIO,'flash',lambda *args:called.append('flashed'))
    monkeypatch.setattr(p4_product,'prepare_storage',lambda *args:None)
    io=SimpleNamespace(args=SimpleNamespace(board='p4xc'),out=tmp_path)
    with step.restoring(path,digest(path)):
        selected=root
        if case=='path':selected=tmp_path/'other';shutil.copytree(root,selected)
        elif case=='elf':manifest['elf_sha256']='b'*64
        else:path.write_text(path.read_text()+'\n')
        with pytest.raises(ValueError):smoke.LiveIO.flash(io,selected,manifest,'restore')
    assert not called


def test_tap_does_not_probe_the_current_epoch():
    class Channel:
        def __init__(self):self.sent=[]
        def send(self,data):self.sent.append(data)
        def emit(self,*args):pass
    stream=io.StringIO();clean=install_tap(Channel,stream,stale_probes=True,clock=lambda:1)
    packet=json.dumps(dict(label='rtvi-ai',type='client-message',data=dict(t='display-ready',d=dict(epoch=11))))
    try:
        first=Channel();second=Channel();first.emit('message',packet);second.emit('message',packet)
        assert not second.sent
    finally:clean()


def test_off_talkover_does_not_require_or_interpret_rome():
    data=events('talkover',False)
    for row in data:
        if row.get('kind')=='aggregator-assistant-turn-stopped':row['text']='One two three ... twenty, not Rome.'
    assert step.turn_checks(data,'talkover',False)['passed']


def test_failed_restore_boot_selects_factory_for_sigusr1_instead_of_retrying_pin(tmp_path,monkeypatch):
    import board_serial
    root,path=pinned(tmp_path);manifest=json.loads((root/'manifest.json').read_text());args=SimpleNamespace(board='p4xc',
        p4_restore_bundle=root,p4_restore_elf=manifest['elf_sha256'])
    device=SimpleNamespace(args=args,out=tmp_path,guard=SimpleNamespace(success=True))
    monkeypatch.setattr(smoke.LiveIO,'flash',lambda *args:None)
    monkeypatch.setattr(p4_product,'prepare_storage',lambda *args:None)
    @contextmanager
    def failed(*args,**kwargs):raise TimeoutError('normal never booted');yield
    monkeypatch.setattr(board_serial,'open_board',failed)
    with step.restoring(path,digest(path)):
        smoke.LiveIO.flash(device,root,manifest,'restore')
        with pytest.raises(TimeoutError):
            with board_serial.open_board('p4xc',log_path=tmp_path/'restore-serial.log'):pass
        assert args.p4_restore_bundle is None and args.p4_restore_elf is None
        assert smoke.restore_decision('p4xc',success=device.guard.success,
            product_restore=bool(args.p4_restore_bundle))=='p4-factory'


@pytest.mark.parametrize('failure',[False,True])
def test_one_shot_boot_proof_uses_factory_hold_until_normal_has_booted(tmp_path,monkeypatch,failure):
    import boards,p4_audio
    root,path=pinned(tmp_path);state=SimpleNamespace(success=False,touched=False,record={},released=False)
    class Hold:
        success=False
        def __enter__(self):return self
        def touch(self):state.touched=True
        def __exit__(self,*exc):
            state.success=self.success;self.record=dict(clean=True,action='keep-tested-p4' if self.success else 'p4-factory')
        def release_signals(self):state.released=True
    class IO:
        def __init__(self,args,manifest):assert args.p4_restore_bundle is None
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*args):assert state.touched
        def open(self):return __import__('contextlib').nullcontext(object())
        def boot(self,board):
            if failure:raise TimeoutError('failed boot')
    monkeypatch.setattr(boards,'require_bench',lambda:0);monkeypatch.setattr(p4_audio,'HardwareIO',IO)
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *args:__import__('contextlib').nullcontext())
    result=step.boot_proof(path,digest(path),tmp_path/'proof',tmp_path/'factory')
    assert result['passed'] is not failure and state.released
    assert result['restore']['action']==('p4-factory' if failure else 'keep-tested-p4')


def test_legacy_runbooks_have_restore_banners_and_no_legacy_live_commands():
    for name in ('p4_product','p4_audio','p4_audio_fix4','p4_headless','p4_display','p4_contention'):
        text=Path(__file__).with_name(name+'.md').read_text()
        assert 'STEP 14 RESTORE GUARD' in text.split('\n\n',3)[1]
        for block in re.findall(r'```(?:bash|sh)\n(.*?)```',text,re.S):
            assert not re.search(r'hil/p4_product\.py\s+hil/',block)


def test_s3_matrix_refuses_dirty_root_lock_before_any_checkout(tmp_path,monkeypatch):
    import subprocess
    scripts=Path(__file__).resolve().parents[1]/'scripts';monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('step14_dirty_lock_test',scripts/'step14_s3.py')
    matrix=importlib.util.module_from_spec(spec);spec.loader.exec_module(matrix);calls=[]
    monkeypatch.setenv('DG_BUILD_LOCK_HELD','1')
    def run(command,**kwargs):
        calls.append(command)
        if command[:2]==['git','diff'] and command[-1]=='dependencies.lock':raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(matrix.subprocess,'run',run)
    with pytest.raises(subprocess.CalledProcessError):
        matrix.main(['--previous',str(tmp_path/'unused'),'--references',str(tmp_path/'unused'),'--out',str(tmp_path)])
    assert len(calls)==1 and calls[0][1]=='diff'


@pytest.mark.parametrize('successful_start',[True,False])
@pytest.mark.parametrize('failed_factory_verify',[False,True])
@pytest.mark.parametrize('failure_phase',['boot','write','storage'])
def test_real_restore_hold_retries_factory_and_retains_ownership_until_verified(tmp_path,monkeypatch,
        successful_start,failed_factory_verify,failure_phase):
    """Real P4Hold/RestoreHold + factory main; only device/process edges fake."""
    import boards,board_serial
    from echoear_rollback import Live
    root,path=pinned(tmp_path);manifest=json.loads((root/'manifest.json').read_text())
    host=Path(__file__).resolve().parents[1]/'spikes/p4_probe/host'
    monkeypatch.syspath_prepend(str(host))
    spec=importlib.util.spec_from_file_location('step14_factory_under_test',host/'restore_factory.py')
    factory=importlib.util.module_from_spec(spec);spec.loader.exec_module(factory)
    state=SimpleNamespace(owned=True,verified=False,factory_boot=False,verify_failed=False,waits=0)
    events=[];decisions=[];factory_file=tmp_path/'factory.bin'
    def bench_fd():assert state.owned;return 123
    monkeypatch.setattr(boards,'require_bench',bench_fd)
    monkeypatch.setattr(factory,'require_bench',bench_fd)
    monkeypatch.setattr(factory,'checked_esptool_python',lambda:'/fake/esptool-python')
    monkeypatch.setattr(factory,'verify_backup',lambda backup:factory_file)
    def run(command,**kwargs):
        assert state.owned and not guard.exited
        if any(str(arg).endswith('restore_factory.py') for arg in command):
            events.append('factory-helper');assert '--backup' in command
            assert command[command.index('--backup')+1]==str(args.p4_factory_backup)
            factory.main(['--backup',str(args.p4_factory_backup)])
        elif 'write-flash' in command:events.append('factory-write')
        elif 'verify-flash' in command:
            events.append('factory-verify')
            if failed_factory_verify and not state.verify_failed:
                state.verify_failed=True;raise subprocess.CalledProcessError(1,command)
            state.verified=True
        elif '-c' in command:
            assert state.verified;events.append('factory-banner');state.factory_boot=True
        else:pytest.fail('unexpected subprocess: '+str(command))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,'run',run)
    def flash(*args):
        events.append('pinned-write')
        if failure_phase=='write':raise OSError('persistent pinned write failure')
    def storage(*args):
        events.append('pinned-verify-and-erase')
        if failure_phase=='storage':raise OSError('persistent storage readback failure')
    monkeypatch.setattr(smoke.LiveIO,'flash',flash)
    monkeypatch.setattr(p4_product,'prepare_storage',storage)
    @contextmanager
    def failed_boot(*args,**kwargs):
        assert state.owned and not guard.exited
        yield SimpleNamespace(cursor=0,reset=lambda:None,
            wait_for=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('pin did not boot')))
    monkeypatch.setattr(board_serial,'open_board',failed_boot)
    decision=smoke.restore_decision
    def decide(*a,**kw):
        result=decision(*a,**kw);decisions.append(result);return result
    monkeypatch.setattr(smoke,'restore_decision',decide)
    def sigusr1_wait(_):
        state.waits+=1;assert state.owned and not guard.exited and not state.factory_boot
        sentinel=failure_phase!='boot'
        assert state.waits<=1+int(sentinel)+int(failed_factory_verify),'unexpected repeated recovery failure'
        if sentinel and state.waits==1:
            assert io.args.p4_restore_bundle==root and guard.success==successful_start
            assert io.args.p4_restore_elf==manifest['elf_sha256']
            # Coordinator selects factory while the real hold is parked. The
            # first resume consumes this; the second must perform recovery.
            (out/'select-factory').touch()
        else:
            assert io.args.p4_restore_bundle is None and io.args.p4_restore_elf is None
            assert guard.success is False
            if sentinel and state.waits==2:
                assert 'factory selected by coordinator' in json.dumps(json.loads((out/'restore.json').read_text()))
                assert 'factory-helper' not in events
        events.append('SIGUSR1-resume')
    monkeypatch.setattr(Live,'wait_recovery',sigusr1_wait)
    args=SimpleNamespace(board='p4xc',p4_restore_bundle=root,p4_restore_elf=manifest['elf_sha256'],
        p4_factory_backup=tmp_path/'backup-pair')  # same fields as liveness parser
    out=tmp_path/'run';out.mkdir()
    io=object.__new__(smoke.LiveIO);io.args=args;io.out=out;io.python='/fake/python'
    with step.restoring(path,digest(path)):
        guard=io.hold();guard.success=successful_start
        try:
            with guard:guard.touch()
            assert guard.exited and state.verified and state.factory_boot
            assert guard.record['action']=='p4-factory' and guard.record['clean'] is False
            assert json.loads((out/'restore.json').read_text())==guard.record
            pinned_attempts=1+int(failure_phase!='boot')
            assert decisions[:pinned_attempts]==['p4-product-of-record']*pinned_attempts
            assert set(decisions[pinned_attempts:])=={'p4-factory'}
            assert events.count('pinned-write')==1
            assert events.count('factory-helper')==1+int(failed_factory_verify)
            assert events[-3:]==['factory-write','factory-verify','factory-banner']
            assert state.waits==pinned_attempts+int(failed_factory_verify)
        finally:guard.release_signals()
    # Modeled outer bench owner may release only after the real hold returns
    # with a verified factory flash and banner. No real bench lock is taken.
    assert state.verified and state.factory_boot;state.owned=False


@pytest.mark.parametrize('backup_valid',[True,False])
def test_product_preflight_also_verifies_factory_pair_before_any_write(tmp_path,monkeypatch,backup_valid):
    root,path=pinned(tmp_path);events=[]
    args=SimpleNamespace(board='p4xc',p4_restore_bundle=root,p4_restore_elf='old',p4_factory_backup=tmp_path/'backup')
    def preflight(io):
        events.append('factory' if io.args.p4_restore_bundle is None else 'product')
        if events[-1]=='factory' and not backup_valid:raise ValueError('bad backup pair')
    monkeypatch.setattr(smoke.LiveIO,'preflight_restore',preflight)
    with step.restoring(path,digest(path)):
        if backup_valid:smoke.LiveIO.preflight_restore(SimpleNamespace(args=args))
        else:
            with pytest.raises(ValueError,match='bad backup pair'):smoke.LiveIO.preflight_restore(SimpleNamespace(args=args))
            assert args.p4_restore_bundle==root and args.p4_restore_elf=='old'
    assert events==(['factory','product'] if backup_valid else ['factory'])


@pytest.mark.parametrize('phase',['record','constructor','tools','restore','hold'])
def test_boot_proof_records_failures_before_device_touch(tmp_path,monkeypatch,phase):
    import boards,p4_audio
    root,path=pinned(tmp_path);monkeypatch.setattr(boards,'require_bench',lambda:123)
    class IO:
        def __init__(self,*args):
            if phase=='constructor':raise ValueError(phase)
        def preflight_tools(self):
            if phase=='tools':raise ValueError(phase)
        def preflight_restore(self):
            if phase=='restore':raise ValueError(phase)
        def hold(self):raise ValueError('hold')
    monkeypatch.setattr(p4_audio,'HardwareIO',IO)
    result=step.boot_proof(path,'0'*64 if phase=='record' else digest(path),tmp_path/'proof',tmp_path/'factory')
    saved=json.loads((tmp_path/'proof/restore-boot-proof.json').read_text())
    assert saved==result and result['passed'] is False and result['device_untouched'] is True
    assert result['restore']['device_untouched'] and 'error' in result


@pytest.mark.parametrize('transient_failure',[False,True])
def test_real_pinned_restore_publishes_product_proof_and_keeps_transient_retry_split(tmp_path,monkeypatch,transient_failure):
    import board_serial
    from echoear_rollback import Live
    root,path=pinned(tmp_path);manifest=json.loads((root/'manifest.json').read_text());calls=[]
    args=SimpleNamespace(board='p4xc',p4_restore_bundle=root,p4_restore_elf=manifest['elf_sha256'],
        p4_factory_backup=tmp_path/'backup')
    out=tmp_path/'run';out.mkdir()
    io=object.__new__(smoke.LiveIO);io.args=args;io.out=out;io.python='/fake/python'
    def flash(*unused):
        calls.append('flash')
        if transient_failure and calls==['flash']:raise OSError('transient verify IO')
    monkeypatch.setattr(smoke.LiveIO,'flash',flash)
    def storage(bundle,out):
        calls.append('verified-and-erased');(out/'candidate-storage-erase.json').write_text(json.dumps(dict(readback_verified=True)))
    monkeypatch.setattr(p4_product,'prepare_storage',storage)
    @contextmanager
    def opened(*args,**kwargs):
        yield SimpleNamespace(cursor=0,reset=lambda:None,wait_for=lambda *a,**kw:'READY transport-console',
            lines_since=lambda at:[])
    monkeypatch.setattr(board_serial,'open_board',opened)
    monkeypatch.setattr(smoke,'boot_check',lambda *args:calls.append('boot-verified'))
    waits=[]
    def wait(_):
        waits.append('resume');assert len(waits)<=1,'unexpected repeated recovery failure'
        assert io.args.p4_restore_bundle==root and guard.success is True and not guard.exited
    monkeypatch.setattr(Live,'wait_recovery',wait)
    with step.restoring(path,digest(path)):
        guard=io.hold();guard.success=True
        try:
            with guard:guard.touch()
            record=json.loads((out/'restore.json').read_text())
            assert record['action']=='p4-product-of-record' and record['clean'] is (not transient_failure)
            assert calls.count('flash')==1+int(transient_failure)
            assert calls[-2:]==['verified-and-erased','boot-verified']
            assert json.loads((out/'restore-storage-001/candidate-storage-erase.json').read_text())['readback_verified']
        finally:guard.release_signals()


@pytest.mark.parametrize('condition',['pass','nonconverged','factory-restore','unclean-restore'])
def test_short_settle_check_records_six_windows_and_requires_pinned_restore(tmp_path,monkeypatch,condition):
    import boards,p4_audio,p4_liveness
    from contextlib import nullcontext
    root,path=pinned(tmp_path);state=SimpleNamespace(reads=0,released=False)
    class Hold:
        success=False
        record=dict(clean=condition!='unclean-restore',action='p4-factory' if condition=='factory-restore' else 'p4-product-of-record')
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def touch(self):pass
        def release_signals(self):state.released=True
    class IO:
        clock=staticmethod(lambda:0)
        def __init__(self,args,manifest):self.args=args
        def preflight_tools(self):pass
        def preflight_restore(self):pass
        def hold(self):return Hold()
        def flash(self,*args):pass
        def open(self):return nullcontext(SimpleNamespace(command=lambda *a:None))
        def boot(self,*args):pass
        def snapshot(self,board):
            from test_p4_liveness import sample
            state.reads+=1;return sample()
    monkeypatch.setattr(boards,'require_bench',lambda:123);monkeypatch.setattr(p4_audio,'HardwareIO',IO)
    monkeypatch.setattr(step,'restoring',lambda *a:nullcontext())
    monkeypatch.setattr(p4_product,'candidate_storage',lambda *a:nullcontext())
    monkeypatch.setattr(step.time,'sleep',lambda *a:None)
    if condition=='nonconverged':
        monkeypatch.setattr(p4_liveness,'settled_heap',lambda *a:(_ for _ in ()).throw(ValueError('not converged')))
    result=step.settle_check(root,path,digest(path),tmp_path/'settle',tmp_path/'backup')
    assert result['passed'] is (condition=='pass') and state.released
    assert result['elf_sha256']==json.loads((root/'manifest.json').read_text())['elf_sha256']
    assert result['record_sha256']==digest(path)
    assert json.loads((tmp_path/'settle/settle-check.json').read_text())==result
    if condition=='pass':
        assert len(result['windows'])==6 and state.reads==18
        assert all(len(window['samples'])==3 and window['converged'] for window in result['windows'])


def test_runbook_stops_on_live_or_offline_inconclusive_exit():
    text=Path(__file__).with_name('p4_liveness.md').read_text();found=[]
    for block in re.findall(r'```bash\n(.*?)```',text,re.S):
        for line in block.splitlines():
            if 'python hil/p4_liveness.py live ' in line or 'python hil/p4_liveness.py score ' in line:
                assert line.endswith('|| exit $?');found.append(line)
    assert len(found)==4
