#!/usr/bin/env python3
"""Coordinator-only duo launcher and process authentication; imports are offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
# Must equal IDENTITY_SOURCES in server/duo_bot.py; test_duo_identity.py compares the two.
SOURCES=('duo_bot.py','duo_route.py','duo_context.py','duo_prompts.py','duo_switch.py','duo_addressee.py','demo_policy.py',
         'demo_interrupts.py','demo_liveness.py','sctp_flight_fix.py','settings.py','aec_session_recorder.py',
         'deepgram_think.py','ice_path.py')


def require(value,message):
    if not value:raise ValueError(message)


# Must equal LLM_URL_VARIABLES in server/duo_bot.py; test_duo_identity.py compares the two.
LLM_URL_VARIABLES=('DUO_LLM_URL','LOCAL_LLM_URL')


def endpoint(url):
    """True for an http(s) URL with a host and no credentials; the value is never put in a message."""
    if type(url) is not str:return False
    try:
        parts=urlsplit(url);parts.port
        return parts.scheme in ('http','https') and bool(parts.hostname) and parts.username is None and parts.password is None
    except ValueError:return False


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path,row):
    with Path(path).open('x') as stream:stream.write(json.dumps(row,sort_keys=True,indent=2,allow_nan=False)+'\n')


def process_token(pid):
    # Same PID/start-time/host-boot identity as board_smoke, without importing audio tools.
    fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
    require(fields[0]!='Z','bot process is a zombie')
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+str(pid)+':'+fields[19]


def validate(manifest,repo=ROOT):
    payload={key:manifest[key] for key in ('config','versions','code_sha256')}
    require(manifest['config_sha256']==hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
            'bot identity digest differs')
    require(manifest['versions']['pipecat-ai']=='1.16.0','unsupported Pipecat version')
    for name in SOURCES:
        require(manifest['code_sha256'][name]==digest(Path(repo)/'server'/name),'bot code differs: '+name)
    config=manifest['config']
    require(config['temperature']==0 and config['enable_thinking'] is False and config['idle_timeout_secs'] is None,
            'duo runtime invariants differ')
    require(config['voices']=={'angel':'flux-hannah-en','devil':'flux-rufus-en'},'duo voices differ')
    require(config['llm_model']=='phonellm-mini-alpha-2-nvfp4','duo model differs')
    # The endpoint is not pinned here, and nothing derived from it is: the repository names no LLM host.
    # What is checked is that one was given on purpose, through the environment, and is an http(s) URL.
    # The record keeps the URL itself (evidence, never git), so two runs can still be compared.
    require(config.get('llm_url_source') in LLM_URL_VARIABLES and endpoint(config.get('llm_url')),
            'duo LLM endpoint not set from the environment: export DUO_LLM_URL')
    require(config.get('context') in ('b2h','labels'),'unknown duo context style')
    require(type(config['duo_test']) is bool and config['fault'] in ('','drop-state-1','drop-state-4','tts-fail-next') and
            (not config['fault'] or config['duo_test']),'invalid fault identity')
    return manifest


def authenticate(directory,repo=ROOT,*,fault=None,token=process_token,cmdline=None):
    directory=Path(directory);identity=json.loads((directory/'identity.json').read_text())
    pid=int((directory/'bot.pid').read_text());require(pid==identity['pid'],'bot PID differs')
    require(token(pid)==identity['bot_instance'],'bot process exited/restarted')
    cmd=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0') if cmdline is None else cmdline
    require(str(Path(repo).resolve()/'server/duo_bot.py').encode() in cmd and b'serve' in cmd,
            'process is not the duo launch adapter')
    require(identity['port']==7861,'duo bot must use port 7861')
    require(identity['adapter_sha256']==digest(Path(repo)/'hil/duo_identity.py'),'launch adapter differs')
    validate(identity['manifest'],repo)
    if fault is not None:
        config=identity['manifest']['config']
        require(config['fault']==fault and config['duo_test']==bool(fault),'unexpected live bot fault configuration')
    ready=json.loads((directory/'ready.json').read_text())
    require(ready['bot_instance']==identity['bot_instance'],'bot not ready for this process')
    return identity


def install_wire_tap(channel_type,stream,clock=time.monotonic_ns):
    """Record actual duo requests/outcomes without altering wire traffic."""
    send,emit=channel_type.send,channel_type.emit
    def observe(direction,data):
        try:row=json.loads(data)
        except (ValueError,TypeError,UnicodeError):return
        if not isinstance(row,dict) or row.get('label')!='rtvi-ai':return
        body=row.get('data',{})
        if not isinstance(body,dict):return
        content=body.get('d') if row.get('type')=='client-message' and body.get('t')=='duo' else body
        if not isinstance(content,dict) or content.get('v')!=1:return
        if row.get('type')=='server-message' and content.get('type')!='duo':return
        if row.get('type') not in ('client-message','server-message'):return
        stream.write(json.dumps(dict(kind='duo-wire',direction=direction,monotonic_ns=clock(),data=content))+'\n');stream.flush()
    def tapped_send(channel,data):
        result=send(channel,data);observe('sent',data);return result
    def tapped_emit(channel,event,*args,**kw):
        if event=='message' and args:observe('received',args[0])
        return emit(channel,event,*args,**kw)
    channel_type.send,channel_type.emit=tapped_send,tapped_emit
    def cleanup():channel_type.send,channel_type.emit=send,emit
    return cleanup


def serve(directory,bot):
    """Executed only by coordinator. The bot's own functions supply the identity."""
    from boards import require_bench
    require_bench()
    directory=Path(directory);bot=Path(bot).resolve()
    require(bot==ROOT/'server/duo_bot.py','this worktree duo bot required')
    sys.path.insert(0,str(bot.parent))
    import duo_bot
    from settings import load_credentials
    from aiortc import RTCDataChannel
    import uvicorn
    load_credentials(approved_only=True)
    # One configuration: the identity records it and the process is started with it.
    config=duo_bot.configuration()
    manifest=validate(duo_bot.identity(config))
    identity=dict(manifest=manifest,pid=os.getpid(),bot_instance=process_token(os.getpid()),port=7861,
                  adapter_sha256=digest(__file__))
    write_new(directory/'identity.json',identity)
    with (directory/'bot.pid').open('x') as stream:stream.write(str(os.getpid())+'\n')
    original=uvicorn.Server.startup
    async def startup(server,*args,**kw):
        result=await original(server,*args,**kw)
        if not server.should_exit:write_new(directory/'ready.json',dict(bot_instance=identity['bot_instance'],monotonic_ns=time.monotonic_ns()))
        return result
    uvicorn.Server.startup=startup
    # Extra service evidence is recorded at the real Flux synthesis boundary.
    run_tts=duo_bot.DuoFluxTTS.run_tts
    push_frame=duo_bot.DuoFluxTTS.push_frame
    from pipecat.frames.frames import TTSAudioRawFrame,ErrorFrame
    with (directory/'voice-events.jsonl').open('x') as voices, (directory/'dc-events.jsonl').open('x') as wire:
        async def synthesis(service,text,context_id):
            row=dict(kind='duo-synthesis',monotonic_ns=time.monotonic_ns(),context_id=context_id,
                     voice=service.settings.voice,conn=service.coordinator.state.conn,rev=service.coordinator.state.rev,
                     character=service.coordinator.state.character)
            voices.write(json.dumps(row)+'\n');voices.flush()
            async for frame in run_tts(service,text,context_id):
                if isinstance(frame,ErrorFrame):
                    voices.write(json.dumps(dict(row,kind='duo-synthesis-error',monotonic_ns=time.monotonic_ns(),
                                                 error=frame.error,error_id=frame.id))+'\n');voices.flush()
                yield frame
        async def audio(service,frame,*args,**kwargs):
            if isinstance(frame,TTSAudioRawFrame):
                context_id=getattr(frame,'context_id',None)
                owner=service.owners.get(context_id)
                row=dict(kind='duo-voice-audio',monotonic_ns=time.monotonic_ns(),context_id=context_id,
                         bytes=len(frame.audio),voice=service.settings.voice,
                         conn=owner[0] if owner else None,rev=owner[1] if owner else None)
                voices.write(json.dumps(row)+'\n');voices.flush()
            return await push_frame(service,frame,*args,**kwargs)
        duo_bot.DuoFluxTTS.run_tts=synthesis;duo_bot.DuoFluxTTS.push_frame=audio
        cleanup=install_wire_tap(RTCDataChannel,wire)
        # run_module reuses the imported class used by the wrapper above.
        from pipecat.runner.run import main as runner_main
        sys.argv=[str(bot),'--host','0.0.0.0','--port','7861']
        main_module=sys.modules["__main__"]
        previous_bot=getattr(main_module,"bot",None)
        main_module.bot=duo_bot.bot
        try:
            # The script's own entry point does this too; the bot is not run as a script here.
            duo_bot.start_process(config)
            runner_main()
        finally:
            if previous_bot is None:del main_module.bot
            else:main_module.bot=previous_bot
            cleanup();duo_bot.DuoFluxTTS.run_tts=run_tts;duo_bot.DuoFluxTTS.push_frame=push_frame;uvicorn.Server.startup=original


def launch(directory,*,fault='',popen=subprocess.Popen):
    from boards import require_bench
    fd=require_bench();directory=Path(directory).resolve()
    require(directory.is_relative_to(Path.home()/'src/evidence/duo/step-7'),'bot output must be under step-7 evidence')
    require(fault in ('','drop-state-1','drop-state-4','tts-fail-next'),'unknown fault')
    directory.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,SERVER_PORT='7861',ECHOEAR_AEC_SESSION_DIR=str(directory),ECHOEAR_STT_PROFILE='demo',
             PIPECAT_SCTP_MAX_CHUNK_SIZE='1100',LOGURU_COLORIZE='NO',PYTHONUNBUFFERED='1',
             OPENBLAS_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1',DUO_TEST='1' if fault else '0',DUO_FAULT=fault)
    command=['uv','run','--project',str(ROOT/'server'),'--frozen','--offline','python',str(Path(__file__).resolve()),
             'serve','--directory',str(directory),'--bot',str(ROOT/'server/duo_bot.py')]
    with (directory/'bot.log').open('x') as log:
        process=popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,pass_fds=(fd,),start_new_session=True)
    return process


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for name in ('launch','serve','check'):
        p=sub.add_parser(name);p.add_argument('--directory',type=Path,required=True)
        if name=='serve':p.add_argument('--bot',type=Path,required=True)
        if name=='launch':p.add_argument('--fault',default='',choices=('','drop-state-1','drop-state-4','tts-fail-next'))
    args=parser.parse_args(argv)
    if args.action=='check':print(json.dumps(authenticate(args.directory)));return 0
    if args.action=='serve':serve(args.directory,args.bot);return 0
    process=launch(args.directory,fault=args.fault)
    def stop(signum,frame):raise KeyboardInterrupt('coordinator stopped bot')
    previous=signal.signal(signal.SIGTERM,stop)
    try:return process.wait()
    except KeyboardInterrupt:
        os.killpg(process.pid,signal.SIGTERM)
        try:return process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);return process.wait(timeout=10)
    finally:signal.signal(signal.SIGTERM,previous)


if __name__=='__main__':raise SystemExit(main())
