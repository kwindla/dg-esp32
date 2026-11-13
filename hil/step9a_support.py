"""Production display setup shared by census, A/B, offline and soak harnesses."""
import json
import os
from pathlib import Path
import socket
import time

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=Path(os.environ.get('STEP9A_FIXTURES',ROOT/'hil/fixtures/step9a'))
BUDGET_PATH=Path(os.environ.get('STEP9A_BUDGET_PATH','/home/khkramer/src/evidence/step9a/resume/arena-budget.json'))


def host_address():
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.connect(('192.168.4.29',1234));return sock.getsockname()[0]


def read(board,action,prefix):
    at=board.cursor;board.command('disp '+action,30)
    lines=[line[2] for line in board.lines_since(at) if line[2].startswith(prefix+' ')]
    if len(lines)!=1:raise RuntimeError('Missing display diagnostic '+prefix)
    return json.loads(lines[0].split(' ',1)[1])


def terminal(board,request_id,timeout=45):
    deadline=time.monotonic()+timeout
    if getattr(board,'display_terminal_events',False) and getattr(board,'display_request_kinds',{}).get(request_id) in ('load','fetch','persist','preload','delete'):
        import re
        cursor=board.display_request_cursors[request_id]
        line=board.wait_for(r'DISPLAY_WORK_RESULT .*"id":"'+re.escape(request_id)+'"',timeout,since=cursor)
        event=json.loads(line.split('DISPLAY_WORK_RESULT ',1)[1])
        return dict(event,state=3 if event['error']=='none' else 4)
    cursor=getattr(board,'display_request_cursors',{}).get(request_id)
    while time.monotonic()<deadline:
        if cursor is not None:
            for _,_,line in board.lines_since(cursor):
                if 'DISPLAY_WORK_RESULT ' in line:
                    event=json.loads(line.split('DISPLAY_WORK_RESULT ',1)[1])
                    if event['id']==request_id:
                        return dict(event,state=3 if event['error']=='none' else 4)
        result=read(board,'result '+request_id,'DISPLAY_RESULT')
        if result['state'] not in (1,2):return result
        time.sleep(.1)
    raise TimeoutError('Display request did not terminate: '+request_id)


def request(board,kind,data,request_id=None):
    at=board.cursor;body=dict(data)
    if request_id:body['id']=request_id
    board.command('disp '+kind+' '+json.dumps(body,separators=(',',':')),30)
    posted=next(json.loads(line[2].split(' ',1)[1]) for line in board.lines_since(at) if line[2].startswith('DISPLAY_POST '))
    if not hasattr(board,'display_request_cursors'):board.display_request_cursors={}
    board.display_request_cursors[posted['id']]=at
    if not hasattr(board,'display_request_kinds'):board.display_request_kinds={}
    board.display_request_kinds[posted['id']]=kind
    return posted


def pack():
    rows=json.loads((FIXTURES/'codec-benchmark.json').read_text())['rows']
    selected={r['source']:r for r in rows if r['codec']==2}
    manifest_path=FIXTURES/'animation.json'
    if not manifest_path.exists():manifest_path=ROOT/'assets/characters/halloween-cat/animation.json'
    manifest=json.loads(manifest_path.read_text())
    names=[manifest['base']]
    for group in ('mouth','eyes','whiskers'):names.extend(manifest['layers'][group]['frames'].values())
    names.append('synthetic-noise')
    return [selected[name] for name in names]


def setup(board,*,full=False,mode=None,rate=30,initialized=False,arena_initialized=False):
    budget=json.loads(BUDGET_PATH.read_text());host=host_address()
    if not initialized:board.command('disp init',30)
    board.command('disp origin http://'+host+':7874')
    if not arena_initialized:board.command('disp arena {budget} {F} {W} {largest}'.format(**budget))
    images=pack()[:17 if full else 16]
    for index,image in enumerate(images):
        rid='fixture-'+str(index)
        request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host+':7874/'+image['hash']+'.dgi'},rid)
        loaded=terminal(board,rid)
        if loaded['error']!='none':raise RuntimeError('Fixture load failed: '+loaded['error'])
        board.command('disp asset '+str(index)+' '+image['hash'])
    if mode:board.command(f'disp bench {mode} {rate}')
    return {'budget':budget,'images':[{'hash':r['hash'],'bytes':r['bytes'],'source':r['source']} for r in images],'status':read(board,'status','DISPLAY')}
