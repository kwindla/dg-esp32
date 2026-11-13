#!/usr/bin/env python3
"""Physical LittleFS commit/reboot/fill checks through the production worker API."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from esp_serial import EspSerial
from step9a_assets import asset_server
from step9a_support import read,request,terminal,host_address,FIXTURES,BUDGET_PATH


def boot(board):
    at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('disp init',45);board.command('disp origin http://'+host_address()+':7874');budget=json.loads(BUDGET_PATH.read_text());board.command('disp arena {budget} {F} {W} {largest}'.format(**budget));return read(board,'status','DISPLAY')


def inventory(board):
    result=[];start=0;revision=0
    while start<256:
        page=read(board,f'inventory {start} {revision}','DISPLAY_INVENTORY');revision=page['revision'];result.extend(page['items']);start=page['next']
    return result


def load(board,image,rid):
    request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},rid);return terminal(board,rid)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False);images=json.loads((FIXTURES/'flash-fixtures.json').read_text());selected={i['name']:i for i in images};result={'passed':False,'tests':{},'owned_images':[]}
    def save():(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    with asset_server(),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log') as board:
      try:
        initial=boot(board);assert initial['flash']['health']=='ready';result['initial']=initial;save()
        for stage,name in ((1,'interrupted-write'),(2,'interrupted-rename'),(3,'interrupted-after-rename')):
            image=selected[name];board.command('disp fault '+str(stage));at=board.cursor;request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},'interrupted-'+str(stage));board.wait_for('display-flash-fault: stage='+str(stage),60,since=at)
            status=boot(board);items=inventory(board);found=next((i for i in items if i[0]==image['hash']),None)
            if stage<3:assert found is None,(stage,found)
            else:
                assert found is not None and found[4] is True,(stage,found);request(board,'preload',{'hash':image['hash']},'reload-committed');ready=terminal(board,'reload-committed');assert ready['error']=='none' and ready['persisted'] is True;result['owned_images'].append(image['hash'])
            assert status['flash']['temp_bytes']==0;result['tests']['interruption-stage-'+str(stage)]={'passed':True,'image':image,'found':found,'status':status};save()
        # Known content is durable and load/preload never rewrites the final file.
        image=selected['interrupted-after-rename'];before=read(board,'status','DISPLAY')['flash'];again=load(board,image,'known-load');after=read(board,'status','DISPLAY')['flash'];assert again['persisted'] and before==after;result['tests']['known-hash-no-rewrite']={'passed':True,'result':again};save()
        # Fill only our new filesystem with valid hash-addressed DGI files. RAM
        # LRU may drop old objects, while the flash inventory must retain them.
        filled=[];failures=[]
        for image in [i for i in images if i['name'].startswith('fill-') or i['name'].startswith('small-fill-')]:
            loaded=load(board,image,'install-'+image['name']);assert loaded['error']=='none';status=read(board,'status','DISPLAY');events=read(board,'persisted','DISPLAY_PERSISTED')
            if loaded['persisted']:
                filled.append(image['hash']);result['owned_images'].append(image['hash'])
            else:
                event=next((e for e in reversed(events['events']) if e[1]==image['hash']),None)
                # The bounded event page may have wrapped; fetch its latest page.
                if event is None:events=read(board,'persisted '+str(max(1,events['total']-5)),'DISPLAY_PERSISTED');event=next((e for e in reversed(events['events']) if e[1]==image['hash']),None)
                assert event and event[2]=='flash_full',event;failures.append({'image':image,'event':event,'status':status});request(board,'delete',{'hash':image['hash']},'discard-'+image['name']);assert terminal(board,'discard-'+image['name'])['error']=='none'
            result['tests']['flash-full']={'passed':bool(failures),'installed':filled,'failures':failures,'status':status};save()
        assert failures and read(board,'status','DISPLAY')['flash']['max_admissible_bytes']<65515
        items=inventory(board);assert all(any(i[0]==h and i[4] for i in items) for h in filled);result['tests']['no-automatic-flash-eviction']={'passed':True,'durable_count':len(filled)};save()
        # Explicit deletion is the only space-recovery path.
        for index,hash in enumerate(result['owned_images']):request(board,'delete',{'hash':hash},'cleanup-'+str(index));assert terminal(board,'cleanup-'+str(index))['error']=='none'
        closed=read(board,'status','DISPLAY');assert closed['flash']['count']==initial['flash']['count'] and closed['flash']['free']>=initial['flash']['free']-65536;result['tests']['explicit-delete-recovery']={'passed':True,'status':closed};result['passed']=True;save()
      finally:
        for command in ('disp fault 0','audio 0','disconnect'):
            try:board.command(command,45)
            except Exception as error:result.setdefault('cleanup_errors',[]).append(str(error))
        save()
    print(json.dumps({'passed':result['passed'],'tests':{k:v['passed'] for k,v in result['tests'].items()}}),flush=True)

if __name__=='__main__':main()
