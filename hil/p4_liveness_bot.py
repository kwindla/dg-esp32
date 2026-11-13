#!/usr/bin/env python3
"""Coordinator launch adapter for the unchanged demo bot; metadata-only DC tap.

Observes every raw pong, including ones demo_liveness silently rejects. An
opt-in stale-epoch fill probe tests the real wire fence without changing pixels.
No device access, process management, policy overrides or audio interception.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import weakref


def packet(data):
    try:
        value=json.loads(data)
        if not isinstance(value,dict) or value.get('label')!='rtvi-ai':return None
        kind=value.get('type');body=value.get('data')
        if not isinstance(body,dict):return None
        if kind=='server-message' and body.get('t')=='ping':return dict(kind='ping',seq=body.get('seq'))
        if kind=='client-message' and body.get('t') in ('pong','display-ready','display-result'):
            content=body.get('d')
            if not isinstance(content,dict):return None
            return dict(kind=body['t'],**{key:content[key] for key in
                ('seq','epoch','next_seq','id','accepted','error') if key in content})
    except (ValueError,TypeError,UnicodeDecodeError):pass
    return None


def install_tap(channel_type, stream, *, stale_probes=False, clock=time.monotonic_ns):
    """Return cleanup; imports/install alone never bind or send on a socket."""
    original_send,original_emit=channel_type.send,channel_type.emit
    channels=weakref.WeakKeyDictionary();previous_epoch=None;counter=0
    def observe(channel,direction,data):
        nonlocal previous_epoch,counter
        row=packet(data)
        if row is None:return
        if channel not in channels:
            counter+=1;channels[channel]=str(os.getpid())+':'+str(counter)
        row.update(channel=channels[channel],direction=direction,monotonic_ns=clock())
        stream.write(json.dumps(row,separators=(',',':'))+'\n');stream.flush()
        if direction=='received' and row['kind']=='display-ready':
            epoch=row.get('epoch');old=previous_epoch;previous_epoch=epoch
            if stale_probes and type(old) is int and old!=epoch:
                rid='step14-stale-'+str(counter)
                probe=dict(v=1,epoch=old,seq=1,id=rid,color=0)
                payload=dict(label='rtvi-ai',type='server-message',data=dict(t='display.fill',d=probe))
                original_send(channel,json.dumps(payload,separators=(',',':')))
                stream.write(json.dumps(dict(kind='stale-display-probe',channel=channels[channel],
                    epoch=old,current_epoch=epoch,id=rid,monotonic_ns=clock()))+'\n');stream.flush()
    def send(channel,data):
        result=original_send(channel,data)
        observe(channel,'sent',data)
        return result
    def emit(channel,event,*args,**kwargs):
        # Record receipt before dispatch, including rejected/late pongs.
        if event=='message' and args:observe(channel,'received',args[0])
        return original_emit(channel,event,*args,**kwargs)
    channel_type.send,channel_type.emit=send,emit
    def cleanup():channel_type.send,channel_type.emit=original_send,original_emit
    return cleanup


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',type=Path,required=True)
    parser.add_argument('--stale-probes',action='store_true')
    parser.add_argument('--ready-file',type=Path,required=True)
    parser.add_argument('--identity',type=Path,required=True)
    parser.add_argument('bot',type=Path);parser.add_argument('arguments',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    if args.ready_file.exists():parser.error('ready marker already exists; use a fresh bot directory')
    bot=args.bot.resolve()
    if bot.name!='demo_bot.py' or bot.parent.name!='server':parser.error('unchanged server/demo_bot.py required')
    from aiortc import RTCDataChannel
    import uvicorn
    from board_smoke import process_token
    sys.path.insert(0,str(bot.parent));sys.argv=[str(bot),*args.arguments]
    from demo_policy import profile_manifest
    repo=bot.parent.parent
    identity=dict(bot_instance=process_token(os.getpid()),profile_manifest=profile_manifest('demo'))
    for field,name in (('bot_source_sha','demo_bot.py'),('policy_source_sha','demo_policy.py')):
        committed=subprocess.check_output(['git','rev-parse','HEAD:server/'+name],cwd=repo,text=True).strip()
        actual=subprocess.check_output(['git','hash-object','server/'+name],cwd=repo,text=True).strip()
        if committed!=actual:raise ValueError('bot source differs: '+name)
        identity[field]=actual
    with args.identity.open('x') as stream:stream.write(json.dumps(identity,indent=2)+'\n')
    startup=uvicorn.Server.startup
    async def observed_startup(server,*a,**kw):
        result=await startup(server,*a,**kw)
        if not server.should_exit:
            pending=args.ready_file.with_name(args.ready_file.name+'.tmp')
            with pending.open('x') as stream:
                stream.write(json.dumps(dict(up_monotonic_ns=time.monotonic_ns(),identity=identity,
                    observer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))+'\n')
            pending.replace(args.ready_file)
        return result
    uvicorn.Server.startup=observed_startup
    with args.events.open('x') as stream:
        cleanup=install_tap(RTCDataChannel,stream,stale_probes=args.stale_probes)
        try:runpy.run_path(str(bot),run_name='__main__')
        finally:cleanup();uvicorn.Server.startup=startup


if __name__=='__main__':main()
