"""Cursor/timestamp journal for the frozen serial reader and pure window replay."""
from collections import deque
from contextlib import contextmanager
import json
from pathlib import Path

from p4_parity import require
from p4_parity_restore import reject_fatal


def candidate_serial(directory):
    """The closed candidate file includes teardown and any unjournaled tail.

    Restore uses separate serial files. Saved run/session windows must never
    shorten this fatal-diagnostic check.
    """
    lines=(Path(directory)/'serial.log').read_text(errors='replace').splitlines()
    reject_fatal(lines,'candidate')
    return lines


@contextmanager
def serial_trace(board,path):
    # The reader already appends under _cv. Preserve its ring and cursor
    # semantics while streaming every entry, including before the first poll.
    with Path(path).open('x') as stream:
        def record(entry):
            cursor,stamp,line=entry
            stream.write(json.dumps(dict(cursor=cursor,monotonic_ns=int(stamp*1e9),line=line),separators=(',',':'))+'\n')
            stream.flush()
        class Traced(deque):
            def append(self,entry):record(entry);super().append(entry)
        with board._cv:
            previous=board._lines
            require(not previous or previous[0][0]==1,'serial trace attached after reader ring overflow')
            for entry in previous:record(entry)
            board._lines=Traced(previous,maxlen=previous.maxlen)
        try:yield
        finally:
            with board._cv:
                previous.clear();previous.extend(board._lines);board._lines=previous


def journal(directory):
    directory=Path(directory)
    rows=[json.loads(line) for line in (directory/'serial-entries.jsonl').read_text().splitlines()]
    require(rows and [r['cursor'] for r in rows]==list(range(1,len(rows)+1)),'serial journal cursors missing/reordered')
    require(all(type(r['monotonic_ns']) is int and r['monotonic_ns']>0 and isinstance(r['line'],str) for r in rows) and
        all(a['monotonic_ns']<=b['monotonic_ns'] for a,b in zip(rows,rows[1:])),'serial journal timestamps invalid/reordered')
    raw=(directory/'serial.log').read_bytes().split(b'\n')[:-1]
    require([line.decode(errors='replace').rstrip() for line in raw[:len(rows)]]==[r['line'] for r in rows],
        'serial journal differs from original serial capture')
    return rows


def window(rows,start,end):
    require(type(start) is int and type(end) is int and 0<=start<end<=len(rows),'serial window cursors invalid')
    chosen=rows[start:end]
    return dict(start_cursor=start,end_cursor=end,start_ns=chosen[0]['monotonic_ns'],end_ns=chosen[-1]['monotonic_ns'])


def select(rows,bounds):
    require(isinstance(bounds,dict) and bounds==window(rows,bounds['start_cursor'],bounds['end_cursor']),
        'serial window timestamp/cursor binding differs')
    return rows[bounds['start_cursor']:bounds['end_cursor']]


def text(rows):return '\n'.join(row['line'] for row in rows)+'\n'


def display_window(directory,a):
    rows=journal(directory);run=select(rows,a['run_window']);active=select(rows,a['wire_window'])
    require(a['run_window']['start_cursor']<=a['wire_window']['start_cursor']<a['wire_window']['end_cursor']<=a['run_window']['end_cursor'],
        'display wire window lies outside run')
    require((Path(directory)/'run-serial.log').read_text()==text(run),'full display serial/window differs')
    require((Path(directory)/'wire.log').read_text()==text(active),'display wire capture differs from original serial window')
    import re
    reject_fatal([r['line'] for r in active],'candidate')
    require(not any(re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE',r['line'],re.I)
        for r in active),'display active session failed')
    return [r['line'] for r in active]


def causal_reset_journal(directory,a,resets):
    import re
    rows=journal(directory);w=a['c6_window'];start=w['start_cursor'];end=a['failure_cursor']
    require(type(start) is int and type(end) is int and 0<start<=end<=len(rows) and
        rows[start-1]['monotonic_ns']==w['start_ns'] and re.search(r'\bSTATE Ready gen=\d+',rows[start-1]['line']) and
        a['failure_ns']>=rows[end-1]['monotonic_ns'],'causal reset window differs from serial journal')
    valid=[r for r in resets if 0<r['cursor']<=len(rows) and rows[r['cursor']-1]['line']==r['line'] and
        rows[r['cursor']-1]['monotonic_ns']==r['host_ns']]
    failures=[r for r in rows[start:end] if re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE',r['line'])]
    return [r for r in valid if not any(f['cursor']<r['cursor'] for f in failures)]


def liveness_sessions(directory,a):
    """Bind generation, boot, original wire, sampled PERF and control epoch."""
    import re
    from p4_parity_liveness import liveness_capture
    from p4_parity_liveness import pinned_bundle
    from board_smoke import boot_check
    from board_smoke import bounds_for
    rows=journal(directory);previous_end=0;boots=[]
    manifest=pinned_bundle(a['bundle'],a['board'],a['variant'])
    for row in rows:
        if 'READY transport-console'==row['line'].strip():boots.append(row['cursor'])
    require(boots,'liveness boot namespace missing')
    previous_boot=0
    for s in a['sessions']:
        segment=select(rows,s['serial_window']);start,end=s['serial_window']['start_cursor'],s['serial_window']['end_cursor']
        require(start>=previous_end,'liveness session serial windows reused/overlap');previous_end=end
        ready=[r for r in segment if re.search(r'\bSTATE Ready gen=\d+\b',r['line'])]
        closed=[r for r in segment if re.search(r'\bPHASE closed\b',r['line'])]
        require(len(ready)==len(closed)==1 and ready[0]['cursor']<closed[0]['cursor'],
            'one actual Ready/close required per liveness session')
        r,c=ready[0],closed[0];generation=int(re.search(r'\bSTATE Ready gen=(\d+)\b',r['line'])[1])
        require(type(s['stop_cursor']) is int and r['cursor']<=s['stop_cursor']<c['cursor'],
            'liveness stop cursor differs')
        require(s['generation']==generation and s['ready_ns']==r['monotonic_ns'] and s['ended_ns']==c['monotonic_ns'] and
            s['ready_cursor']==r['cursor'] and s['close_cursor']==c['cursor'],'liveness Ready/close generation/timestamp binding differs')
        boot=max((cursor for cursor in boots if cursor<r['cursor']),default=0)
        require(boot and s['boot_cursor']==boot,'liveness boot cursor differs')
        before_boot=[entry for entry in rows[:boot] if 'elf_sha256=' in entry['line']]
        require(before_boot,'liveness boot ELF missing')
        boot_check([entry['line'] for entry in rows[before_boot[-1]['cursor']-1:boot]],
            manifest,a['board'])
        if previous_boot:
            require((s['boot']==previous_namespace if boot==previous_boot else s['boot']==previous_namespace+1),
                'liveness boot namespace differs')
        previous_boot,previous_namespace=boot,s['boot']
        # PERF is emitted on demand while Ready, before intentional teardown.
        perf=[]
        for entry in segment:
            if entry['line'].startswith('PERF '):
                value=json.loads(entry['line'][5:])
                if r['cursor']<=entry['cursor']<=c['cursor']:perf.append(value)
        require(s['samples'] and all(sample in perf and sample.get('generation')==generation and
            sample.get('pc_id') for sample in s['samples']),'resource sample detached from serial generation')
        require(len({p['pc_id'] for p in s['samples']})==1,'resource samples span peers')
        epoch=[json.loads(entry['line'][8:]).get('epoch') for entry in segment if entry['line'].startswith('DISPLAY ')]
        require(s['epoch'] in epoch,'control channel epoch not witnessed by this serial session')
        lines=[r['line'] for r in segment]
        require((Path(directory)/(s['label']+'-serial.log')).read_text()==text(segment),
            'liveness session capture differs from original serial window')
        bot=(Path(directory)/(s['label']+'-bot.log')).read_text().splitlines()
        _,wire,_=liveness_capture(lines,[line for line in bot if 'DEMO_CAPABILITY ' in line],a['board'],
            bounds_for(a['bounds_path'],a['variant']),aec=a['aec_enabled'])
        require((Path(directory)/s['wire_file']).read_text()=='\n'.join(wire)+'\n',
            'liveness wire differs from original serial session')
        from wire_contract import parse_capture
        parsed=parse_capture(wire)
        require(parsed.intervals and all(interval['gen']==generation for interval in parsed.intervals),
            'liveness wire interval generation differs from Ready')
        active=[entry['line'] for entry in segment if r['cursor']<=entry['cursor']<=s['stop_cursor']]
        require(not any(re.search(r'SESSION FAIL|Peer disconnected|RTCP_BYE',line,re.I) for line in active),
            'liveness active session failed')
    return dict(sessions=len(a['sessions']))
