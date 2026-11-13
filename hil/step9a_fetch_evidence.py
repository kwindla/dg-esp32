"""Retained worker completion evidence after bounded result-cache eviction."""
import json
import re


def completion(serial_text, request_id='in-call-fetch', expected_bytes=306917):
    lines=serial_text.splitlines()
    admitted=[i for i,line in enumerate(lines) if line.startswith('DISPLAY_POST ') and
              json.loads(line.split(' ',1)[1]).get('id')==request_id]
    if len(admitted)!=1:
        raise ValueError('Expected exactly one admitted fetch request')
    pattern=re.compile(r'load_terminal epoch=(\d+) order=(\d+) error=(\S+) bytes=(\d+) duration_us=(\d+) persisted=([01])')
    events=[m for line in lines[admitted[0]+1:] if (m:=pattern.search(line))]
    if len(events)!=1:
        raise ValueError('Expected exactly one subsequent worker load terminal')
    epoch,order,error,received,duration,persisted=events[0].groups()
    if error!='none' or int(received)!=expected_bytes or persisted!='0' or int(duration)<=0:
        raise ValueError('In-call fetch did not complete with validated RAM-only bytes: '+events[0].group())
    return {'id':request_id,'state':3,'error':error,'persisted':False,'epoch':int(epoch),
            'worker_order':int(order),'bytes':int(received),'worker_duration_us':int(duration),
            'decimal_kB_per_s':int(received)*1000/int(duration),
            'basis':'single admitted request followed by worker terminal after full SHA/DGI validation; no late query of evictable results; retained serial evidence'}
