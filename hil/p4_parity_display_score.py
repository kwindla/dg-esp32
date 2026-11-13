"""Pure display replay and scoped acquisition witnesses; no device IO on import."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from p4_parity import load,digest,require,pinned_bundle,clean_result


def records(lines,prefix):
    return [json.loads(line.split(prefix+' ',1)[1]) for line in lines if line.startswith(prefix+' ')]


def fixture_assets():
    base=Path(__file__).resolve().parent/'fixtures/echoear360'
    selected={r['source']:r for r in load(base/'codec-benchmark.json')['rows'] if r['codec']==2}
    animation=base/'animation.json'
    if not animation.exists():animation=base.parents[2]/'assets/characters/halloween-cat/animation.json'
    manifest=load(animation);names=[manifest['base']]
    for group in ('mouth','eyes','whiskers'):names+=list(manifest['layers'][group]['frames'].values())
    return base,[selected[name] for name in names+['synthetic-noise']]


@contextmanager
def command_trace(board,path):
    previous=board.command
    entries=[]
    with Path(path).open('x') as stream:
        def command(text,*args,**kwargs):
            at=board.cursor;row=dict(command=text,started_ns=time.monotonic_ns(),start_cursor=at)
            try:return previous(text,*args,**kwargs)
            except BaseException as exc:row['error']=type(exc).__name__+': '+str(exc);raise
            finally:
                row.update(ended_ns=time.monotonic_ns(),end_cursor=board.cursor,response=[line for _,_,line in board.lines_since(at)])
                entries.append(row)
                stream.write(json.dumps(row,separators=(',',':'))+'\n');stream.flush()
        board.command=command
        try:yield entries
        finally:board.command=previous


@contextmanager
def http_trace(server,path):
    """Record bytes actually written after headers; preserve server behavior."""
    handler=server.RequestHandlerClass;original=handler.do_GET;lock=threading.Condition();pending=0
    with Path(path).open('x') as stream:
        def get(self):
            nonlocal pending
            with lock:pending+=1
            row=dict(path=self.path,started_ns=time.monotonic_ns());body=False;size=0;sha=hashlib.sha256()
            old_write=self.wfile;old_response=self.send_response;old_headers=self.end_headers;old_header=self.send_header
            class Sink:
                def write(inner,data):
                    nonlocal size
                    result=old_write.write(data)
                    if body:sha.update(data);size+=len(data)
                    return result
                def __getattr__(inner,name):return getattr(old_write,name)
            def response(code,message=None):row['status']=code;return old_response(code,message)
            def header(name,value):
                if name.lower()=='content-length':row['content_length']=int(value)
                return old_header(name,value)
            def headers():
                nonlocal body
                result=old_headers();body=True;return result
            self.wfile=Sink();self.send_response=response;self.send_header=header;self.end_headers=headers
            try:return original(self)
            finally:
                self.wfile=old_write;self.send_response=old_response;self.send_header=old_header;self.end_headers=old_headers
                row.update(ended_ns=time.monotonic_ns(),body_bytes=size,body_sha256=sha.hexdigest())
                with lock:
                    stream.write(json.dumps(row,separators=(',',':'))+'\n');stream.flush()
                    pending-=1;lock.notify_all()
        handler.do_GET=get
        try:yield
        finally:
            handler.do_GET=original
            with lock:lock.wait_for(lambda:pending==0)


def terminal_rows(lines,rid):
    work=[dict(row,state=3 if row['error']=='none' else 4) for row in records(lines,'DISPLAY_WORK_RESULT') if row.get('id')==rid]
    return work+[row for row in records(lines,'DISPLAY_RESULT') if row.get('id')==rid and row.get('state') not in (1,2)]


def command_body(entries,operation,rid):
    matches=[]
    for row in entries:
        if row['command'].startswith('disp '+operation+' '):
            body=json.loads(row['command'].split(' ',2)[2])
            if body.get('id')==rid:matches.append(body)
    require(len(matches)==1,'missing/ambiguous retained '+operation+' command '+rid)
    return matches[0]


def score_display(directory,board,variant):
    from board_smoke import boot_check
    from wire_contract import parse_capture,validate_capture,identity_schema
    from echoear_status_assets import status_assets
    from display_session import display_session,display_sent,display_result,stale_bridge_sends
    from step9a_merge_checks import rendered_show
    from p4_parity_restore import restore_proof
    directory=Path(directory).resolve();a=load(directory/'acquisition.json');clean_result(a)
    manifest=pinned_bundle(a['bundle'],board,variant)
    require(a.get('board')==board and a.get('variant')==variant and a.get('elf_sha256')==manifest['elf_sha256'] and
        load(directory/'image.json')==manifest,'display image/summary differs')
    require(not (directory/'analysis-error.json').exists(),'display analysis failed')
    lines=(directory/'run-serial.log').read_text().splitlines();boot_check(lines,manifest,board)
    from p4_parity_serial import display_window,candidate_serial
    candidate_serial(directory)
    wire_lines=display_window(directory,a)
    if board=='p4xc':require(any('800x800 RGB565 LE framebuffer PSRAM=1 DPI=40MHz' in l for l in lines),'DSI boot witness missing')
    identity=load(directory/'identity.json');identity_schema(identity)
    events=load(directory/'events.json');require(isinstance(events,list) and events,'display events missing')
    commands=[json.loads(line) for line in (directory/'commands.jsonl').read_text().splitlines()]
    require(commands and all(not c.get('error') and type(c.get('started_ns')) is int and
        type(c.get('ended_ns')) is int and c['ended_ns']>=c['started_ns'] for c in commands),'command witness incomplete')
    connect=[c for c in commands if c['command']=='connect'];require(len(connect)==1,'one display connect required')
    cleanup=next((c for c in commands if c['command']=='disp character off' and c['start_cursor']>connect[0]['start_cursor']),None)
    require(cleanup and a['wire_window']['start_cursor']==connect[0]['start_cursor'] and
        a['wire_window']['end_cursor']==cleanup['start_cursor'],'display connect/cleanup window differs')
    cursor=0
    for command in commands:
        for line in command['response']:
            try:cursor=lines.index(line,cursor)+1
            except ValueError:raise ValueError('command response is absent/reordered in raw serial')
    proofs=[p for row in records(lines,'DISPLAY_DEMO_PROOF') for p in row.get('items',[])]
    require([s.get('screen') for s in a.get('screens',[])]==['disconnected','connecting','live'],'three committed screens required')
    previous=dict(n=0,completed_us=0,revision=0,rendered=0)
    for number,saved in enumerate(a['screens']):
        proof=saved.get('proof',{})
        require(proof in proofs and proof.get('screen')==number and
            all(type(proof.get(k)) is int and proof[k]>previous[k] for k in previous),
            'missing/contradictory committed screen proof')
        require(any(c['command']=='disp demo-screen '+saved['screen'] for c in commands),'screen command missing')
        previous={k:proof[k] for k in previous}
    fixtures,assets=fixture_assets();images=assets[:16];statuses=status_assets()
    for image in [*images,*statuses,assets[-1]]:
        file=fixtures/(image['hash']+'.dgi')
        require(file.stat().st_size==image['bytes'] and digest(file)==image['hash'],'frozen asset hash/size differs')
    def operation(kind,rid,image,persist=False):
        body=command_body(commands,kind,rid)
        require(body.get('hash')==image['hash'],'asset hash differs')
        if kind=='load':
            require(body.get('bytes')==image['bytes'] and urlsplit(body['url']).path=='/'+image['hash']+'.dgi','asset request differs')
        results=terminal_rows(lines,rid)
        require(results and all(r.get('error')=='none' and r.get('state')==3 for r in results),'asset completion missing/failed')
        if persist:require(all(r.get('persisted') is True for r in results),'persistence not confirmed')
        return results
    for i,image in enumerate(images):operation('load','fixture-'+str(i),image)
    require(len(a.get('status_assets',[]))==len(statuses),'status persistence summary missing')
    inventory=[item for page in records(lines,'DISPLAY_INVENTORY') for item in page.get('items',[])]
    for i,image in enumerate(statuses):
        operation('load','status-load-'+str(image['slot']),image)
        persisted=operation('persist','status-persist-'+str(image['slot']),image,True)
        require(a['status_assets'][i] in persisted,'status persistence summary differs')
        require(any(row[0]==image['hash'] and row[1]==image['bytes'] and row[4:6]==[True,True] and row[6]=='none' for row in inventory),
            'status hash not present in raw persisted inventory')
    probe=assets[-1];operation('delete','step16-http-delete',probe);operation('load','step16-http-load',probe)
    http=[json.loads(line) for line in (directory/'http.jsonl').read_text().splitlines()]
    require(any(row.get('path')=='/'+probe['hash']+'.dgi' and row.get('status')==200 and
        row.get('content_length')==probe['bytes'] and row.get('body_bytes')==probe['bytes'] and
        row.get('body_sha256')==probe['hash'] for row in http),'HTTP asset body/status/length/hash not witnessed')
    wire=parse_capture(wire_lines)
    validate_capture(wire,profile='bench-display',intended_aec=manifest['aec_enabled'],target=manifest['target'],request_id='p4-display-load')
    require(any(s.get('epoch')==a['display_session']['epoch'] for s in records(lines,'DISPLAY')),'raw session epoch missing')
    session=display_session(events,a['display_session']['epoch'])
    require(session==a['display_session'] and not stale_bridge_sends(events,session),'display session/stale bridge differs')
    require([r.get('operation') for r in a.get('wire_requests',[])]==['load','show'],'load/show originals missing')
    for saved,operation_name,rid,body in zip(a['wire_requests'],('load','show'),('p4-display-load','p4-display-show'),
        (dict(hash=images[0]['hash'],bytes=images[0]['bytes']),dict(hash=images[0]['hash'],layer=7,x=0,y=0))):
        sent=display_sent(events,session,rid)
        require(sent is not None and sent['packet'].get('t')=='display.'+operation_name and
            all(sent['packet']['d'].get(k)==v for k,v in body.items()),'load/show request body/session differs')
        ack=display_result(events,session,sent['packet']['d'])
        require(ack is not None and ack['result'].get('accepted') is True and not ack['result'].get('parse_failed'),
            'display acknowledgement missing/rejected')
        completed=saved.get('completed')
        require(saved.get('sent')==sent and saved.get('ack')==ack and completed in terminal_rows(lines,rid) and
            completed.get('error')=='none','raw terminal result missing/contradictory')
        if operation_name=='show':
            status=saved.get('status');layers=saved.get('layers')
            require(status in records(lines,'DISPLAY') and layers in records(lines,'DISPLAY_LAYERS') and
                rendered_show(completed,status,layers,body),'show revision/layer not rendered')
    sources=restore_proof(directory,a)['sources']
    for name in ('acquisition.json','image.json','run-serial.log','serial.log','serial-entries.jsonl','wire.log','events.json','commands.jsonl','http.jsonl','identity.json'):
        path=directory/name;sources[str(path)]=digest(path)
    return dict(checks_passed=True,variant=variant,elf_sha256=manifest['elf_sha256'],sources=sources,qualification=False)
