"""Offline process and launch contract tests; no service is started."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import duo_identity as identity


def manifest():
    payload=dict(config=dict(temperature=0,enable_thinking=False,idle_timeout_secs=None,
        voices=dict(angel='flux-hannah-en',devil='flux-rufus-en'),llm_model='phonellm-max-alpha-2-nvfp4',
        llm_url='http://llm.invalid:8000/v1',llm_url_source='DUO_LLM_URL',duo_test=False,fault=''),versions={'pipecat-ai':'1.15.0','deepgram-sdk':'6'},
        code_sha256={n:identity.digest(identity.ROOT/'server'/n) for n in identity.SOURCES})
    return sign(payload)


def sign(payload):
    payload.pop('config_sha256',None)
    return dict(payload,config_sha256=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest())


def record(tmp_path):
    row=dict(manifest=manifest(),pid=42,bot_instance='boot:42:start',port=7861,adapter_sha256=identity.digest(identity.__file__))
    identity.write_new(tmp_path/'identity.json',row);(tmp_path/'bot.pid').write_text('42')
    identity.write_new(tmp_path/'ready.json',dict(bot_instance=row['bot_instance']))
    return row


def test_authenticates_live_process_and_all_recorded_fields(tmp_path):
    row=record(tmp_path)
    result=identity.authenticate(tmp_path,token=lambda pid:row['bot_instance'],cmdline=[b'python',b'serve',str(identity.ROOT/'server/duo_bot.py').encode()],fault='')
    assert result==row and set(identity.SOURCES)<=result['manifest']['code_sha256'].keys()


def test_launcher_and_bot_bind_the_same_sources():
    import ast
    tree=ast.parse((identity.ROOT/'server/duo_bot.py').read_text())
    bound=[ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
           and [getattr(t,'id',None) for t in node.targets]==['IDENTITY_SOURCES']]
    assert len(bound)==1 and set(bound[0])==set(identity.SOURCES) and len(bound[0])==len(identity.SOURCES)
    assert all((identity.ROOT/'server'/name).is_file() for name in identity.SOURCES)


@pytest.mark.parametrize('name',identity.SOURCES)
def test_every_bound_source_is_checked(name):
    m=manifest();m['code_sha256'][name]='0'*64
    with pytest.raises(ValueError,match='bot code differs: '+name):identity.validate(sign(m))
    m=manifest();del m['code_sha256'][name]
    with pytest.raises(KeyError):identity.validate(sign(m))


@pytest.mark.parametrize('field',['code','voices','model','endpoint','source','version','fault','digest','idle'])
def test_bad_identity_fails_closed(field):
    m=manifest()
    if field=='code':m['code_sha256']['duo_switch.py']='0'*64
    if field=='voices':m['config']['voices']['angel']='wrong'
    if field=='model':m['config']['llm_model']='wrong'
    if field=='endpoint':m['config']['llm_url']=None
    if field=='source':m['config']['llm_url_source']=None
    if field=='version':m['versions']['pipecat-ai']='2'
    if field=='fault':m['config']['fault']='drop-state-1'
    if field=='idle':m['config']['idle_timeout_secs']=300
    m=sign(m)
    if field=='digest':m['config_sha256']='0'*64
    with pytest.raises(ValueError):identity.validate(m)


def test_endpoint_is_whatever_the_environment_named_and_nothing_is_pinned():
    """No host is written in the repository, so any endpoint given through a known variable is accepted."""
    for url,source in (('http://other.invalid:9/v1','DUO_LLM_URL'),('https://llm.invalid/v1','LOCAL_LLM_URL')):
        m=manifest();m['config'].update(llm_url=url,llm_url_source=source)
        assert identity.validate(sign(m))['config']['llm_url']==url
    for url,source in (('','DUO_LLM_URL'),(None,None),('http://llm.invalid:8000/v1','OTHER_URL'),
                       ('http://llm.invalid:8000/v1',None),('llm.invalid:8000','DUO_LLM_URL'),
                       ('ftp://llm.invalid/v1','DUO_LLM_URL'),('http://llm.invalid:port/v1','DUO_LLM_URL'),
                       ('http://user:word@llm.invalid/v1','DUO_LLM_URL'),(7,'DUO_LLM_URL')):
        m=manifest();m['config'].update(llm_url=url,llm_url_source=source)
        with pytest.raises(ValueError,match='export DUO_LLM_URL') as error:identity.validate(sign(m))
        assert 'invalid' not in str(error.value)
    m=manifest();del m['config']['llm_url_source']
    with pytest.raises(ValueError,match='export DUO_LLM_URL'):identity.validate(sign(m))
    source=(identity.ROOT/'hil/duo_identity.py').read_text()
    assert '://' not in source  # no endpoint, and no place to hide one


def test_launcher_and_bot_read_the_endpoint_from_the_same_variables():
    import ast
    tree=ast.parse((identity.ROOT/'server/duo_bot.py').read_text())
    bound=[ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
           and [getattr(t,'id',None) for t in node.targets]==['LLM_URL_VARIABLES']]
    assert bound==[identity.LLM_URL_VARIABLES]


@pytest.mark.parametrize('bad',['token','command','ready','fault','pid','adapter','port'])
def test_refuses_unrelated_restarted_or_changed_process(tmp_path,bad):
    row=record(tmp_path);cmd=[b'serve',str(identity.ROOT/'server/duo_bot.py').encode()]
    token=lambda pid:row['bot_instance']
    if bad=='token':token=lambda pid:'other'
    if bad=='command':cmd=[b'serve',b'/other/duo_bot.py']
    if bad=='ready':(tmp_path/'ready.json').write_text('{"bot_instance":"old"}')
    if bad=='pid':(tmp_path/'bot.pid').write_text('44')
    if bad in ('adapter','port'):
        row[{'adapter':'adapter_sha256','port':'port'}[bad]]=0;(tmp_path/'identity.json').write_text(json.dumps(row))
    with pytest.raises(ValueError):identity.authenticate(tmp_path,token=token,cmdline=cmd,fault='drop-state-1' if bad=='fault' else '')


def test_wire_tap_is_transparent_and_records_requests_and_state():
    import io
    stream=io.StringIO();seen=[]
    class Channel:
        def send(self,data):seen.append(('send',data));return 17
        def emit(self,*args,**kw):seen.append(('emit',args));return 18
    old_send=Channel.send;channel=Channel();cleanup=identity.install_wire_tap(Channel,stream,clock=lambda:123)
    request=dict(v=1,op='toggle',session='s',seq=2,have='angel')
    packet=json.dumps(dict(label='rtvi-ai',type='client-message',data=dict(t='duo',d=request)))
    assert channel.emit('message',packet)==18
    assert channel.send(json.dumps(dict(label='rtvi-ai',type='server-message',data=dict(type='duo',v=1,op='state'))))==17
    channel.emit('message','bad-json');channel.send('{}');cleanup()
    rows=[json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows)==2 and rows[0]['data']==request and rows[0]['monotonic_ns']==123
    assert Channel.send is old_send and len(seen)==4


def test_launch_uses_uv_credentials_adapter_and_inherited_lock(tmp_path,monkeypatch):
    import boards
    monkeypatch.setattr(boards,'require_bench',lambda:99)
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    captured=[]
    def popen(command,**kwargs):captured.append((command,kwargs));return 'fake-process'
    out=tmp_path/'src/evidence/duo/step-7/bot'
    assert identity.launch(out,fault='drop-state-4',popen=popen)=='fake-process'
    command,kw=captured[0]
    assert command[:3]==['uv','run','--project'] and command[-1]==str(identity.ROOT/'server/duo_bot.py')
    assert kw['pass_fds']==(99,) and kw['env']['DUO_TEST']=='1' and kw['env']['DUO_FAULT']=='drop-state-4'
    assert kw['env']['SERVER_PORT']=='7861' and kw['env']['ECHOEAR_AEC_SESSION_DIR']==str(out)
    with pytest.raises(FileExistsError):identity.launch(out,popen=popen)


def test_launch_refuses_without_bench_before_creating_outputs(tmp_path,monkeypatch):
    import boards
    def refuse():raise RuntimeError('bench lock')
    monkeypatch.setattr(boards,'require_bench',refuse)
    with pytest.raises(RuntimeError):identity.launch(tmp_path/'never')
    assert not (tmp_path/'never').exists()


def test_serve_uses_bot_identity_loader_and_runner_without_live_io(tmp_path,monkeypatch):
    import asyncio
    import sys
    import boards
    from types import SimpleNamespace
    seen=[];m=manifest()
    from pipecat.frames.frames import ErrorFrame,TTSAudioRawFrame
    fault=ErrorFrame(error='DUO_FAULT tts-fail-next')
    class TTS:
        async def push_frame(self,*args):pass
        async def run_tts(self,text,context_id):
            yield 'frame'
            yield fault
    async def bot(args):pass
    def identity_of(config):
        assert config is m['config']
        return m
    fake_bot=SimpleNamespace(configuration=lambda:m['config'],identity=identity_of,DuoFluxTTS=TTS,bot=bot,
                             start_process=lambda config:seen.append(('process',config is m['config'])))
    class Channel:
        def send(self,data):pass
        def emit(self,*args):pass
    class Server:
        async def startup(self):seen.append('startup')
    monkeypatch.setattr(boards,'require_bench',lambda:9)
    monkeypatch.setitem(sys.modules,'duo_bot',fake_bot)
    monkeypatch.setitem(sys.modules,'settings',SimpleNamespace(load_credentials=lambda **kw:seen.append(kw)))
    monkeypatch.setitem(sys.modules,'aiortc',SimpleNamespace(RTCDataChannel=Channel))
    monkeypatch.setitem(sys.modules,'uvicorn',SimpleNamespace(Server=Server))
    original=TTS.run_tts;original_startup=Server.startup
    original_argv=sys.argv
    def run():
        assert sys.modules['__main__'].bot is bot
        assert sys.argv[-2:]==['--port','7861']
        asyncio.run(Server.startup(SimpleNamespace(should_exit=False)))
        service=TTS();service.settings=SimpleNamespace(voice='flux-hannah-en');service.owners={}
        service.coordinator=SimpleNamespace(state=SimpleNamespace(conn='epoch',rev=1,character='angel'))
        async def consume():return [f async for f in service.run_tts('report-only','ctx')]
        assert asyncio.run(consume())==['frame',fault]
        asyncio.run(service.push_frame(TTSAudioRawFrame(audio=b'\0'*640,sample_rate=16000,num_channels=1,context_id='old-restored-context')))
    monkeypatch.setitem(sys.modules,'pipecat.runner.run',SimpleNamespace(main=run))
    try:identity.serve(tmp_path,identity.ROOT/'server/duo_bot.py')
    finally:sys.argv=original_argv
    # The process is started, with the configuration the identity records, before the server is.
    assert seen==[{'approved_only':True},('process',True),'startup']
    assert json.loads((tmp_path/'identity.json').read_text())['manifest']==m
    rows=[json.loads(line) for line in (tmp_path/'voice-events.jsonl').read_text().splitlines()]
    assert rows[0]['voice']=='flux-hannah-en'
    assert rows[1]['kind']=='duo-synthesis-error' and rows[1]['context_id']=='ctx' and rows[1]['error_id']==fault.id
    assert rows[2]['kind']=='duo-voice-audio' and rows[2]['context_id']=='old-restored-context' and rows[2]['bytes']==640
    assert (tmp_path/'ready.json').is_file() and TTS.run_tts is original and Server.startup is original_startup


@pytest.mark.parametrize('log,freeze',[(False,False),(True,False),(False,True),(True,True)])
def test_serve_applies_the_gc_flags_its_identity_records(tmp_path,monkeypatch,log,freeze):
    """The coordinator's launch path with the real bot module: its configuration, identity and
    process start. Only what would touch a credential, a socket or the collector is replaced."""
    import os
    import sys
    import boards
    import duo_bot
    for name in list(os.environ):
        if name.startswith('DUO_') or name in ('LOCAL_LLM_URL','LOCAL_LLM_MODEL'):monkeypatch.delenv(name)
    monkeypatch.setenv('DUO_LLM_URL','http://llm.invalid:8000/v1')
    if log:monkeypatch.setenv('DUO_GC_LOG','1')
    if freeze:monkeypatch.setenv('DUO_GC_FREEZE','1')
    steps=[]
    monkeypatch.setattr(duo_bot,'_process_started',False)
    monkeypatch.setattr(duo_bot,'SlowCollections',lambda:SimpleNamespace(start=lambda:steps.append('log')))
    monkeypatch.setattr(duo_bot,'freeze_startup_heap',lambda:steps.append('freeze'))
    class Channel:
        def send(self,data):pass
        def emit(self,*args):pass
    class Server:
        async def startup(self):raise AssertionError('no server may start')
    monkeypatch.setattr(boards,'require_bench',lambda:9)
    monkeypatch.setitem(sys.modules,'settings',SimpleNamespace(load_credentials=lambda **kw:None))
    monkeypatch.setitem(sys.modules,'aiortc',SimpleNamespace(RTCDataChannel=Channel))
    monkeypatch.setitem(sys.modules,'uvicorn',SimpleNamespace(Server=Server))
    monkeypatch.setitem(sys.modules,'pipecat.runner.run',SimpleNamespace(main=lambda:steps.append('runner')))
    tts=(duo_bot.DuoFluxTTS.run_tts,duo_bot.DuoFluxTTS.push_frame);original_argv=sys.argv
    try:identity.serve(tmp_path,identity.ROOT/'server/duo_bot.py')
    finally:sys.argv=original_argv
    recorded=json.loads((tmp_path/'identity.json').read_text())['manifest']['config']
    assert (recorded['gc_log'],recorded['gc_freeze'])==(log,freeze)
    assert (recorded['llm_url'],recorded['llm_url_source'])==('http://llm.invalid:8000/v1','DUO_LLM_URL')
    # Each flag is applied once, the log first, and before the runner binds anything.
    assert steps==['log']*log+['freeze']*freeze+['runner']
    assert duo_bot._process_started and (duo_bot.DuoFluxTTS.run_tts,duo_bot.DuoFluxTTS.push_frame)==tts


@pytest.mark.parametrize('fallback',[False,True])
def test_serve_needs_an_llm_endpoint_from_the_environment(tmp_path,monkeypatch,fallback):
    """The real bot module through serve(): with no endpoint variable nothing is recorded and nothing starts;
    LOCAL_LLM_URL alone is enough and is recorded as the source."""
    import os
    import sys
    import boards
    import duo_bot
    for name in list(os.environ):
        if name.startswith('DUO_') or name in ('LOCAL_LLM_URL','LOCAL_LLM_MODEL'):monkeypatch.delenv(name)
    if fallback:monkeypatch.setenv('LOCAL_LLM_URL','http://llm.invalid:8000/v1')
    steps=[]
    monkeypatch.setattr(duo_bot,'_process_started',False)
    class Channel:
        def send(self,data):pass
        def emit(self,*args):pass
    class Server:
        async def startup(self):raise AssertionError('no server may start')
    monkeypatch.setattr(boards,'require_bench',lambda:9)
    monkeypatch.setitem(sys.modules,'settings',SimpleNamespace(load_credentials=lambda **kw:None))
    monkeypatch.setitem(sys.modules,'aiortc',SimpleNamespace(RTCDataChannel=Channel))
    monkeypatch.setitem(sys.modules,'uvicorn',SimpleNamespace(Server=Server))
    monkeypatch.setitem(sys.modules,'pipecat.runner.run',SimpleNamespace(main=lambda:steps.append('runner')))
    original_argv=sys.argv
    try:
        if fallback:identity.serve(tmp_path,identity.ROOT/'server/duo_bot.py')
        else:
            with pytest.raises(ValueError,match='export DUO_LLM_URL'):identity.serve(tmp_path,identity.ROOT/'server/duo_bot.py')
    finally:sys.argv=original_argv
    if fallback:
        assert steps==['runner']
        assert json.loads((tmp_path/'identity.json').read_text())['manifest']['config']['llm_url_source']=='LOCAL_LLM_URL'
    else:
        assert steps==[] and not duo_bot._process_started and not list(tmp_path.iterdir())
