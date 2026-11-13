"""Real-width split console records through the live IO parser, without serial."""
import json
import pytest
import duo_smoke as smoke
from test_duo_smoke import proof,state
from duo_touch_verify import status_matches

U32=2**32-1
U64=2**64-1
FAULTS=('malformed','session','connection','baseline','stale-revision','mailbox','send','timeout',
        'render','switch-failed','mismatch','stale','voice','deadline','cancelled','other')


def line(prefix,row):return prefix+' '+json.dumps(row,separators=(',',':'))


def proof_lines():
    row=proof(session='f'*16,generation=U32,conn='c'*128,seq=U32,rev=U64,scene_revision=U64)
    header={k:v for k,v in row.items() if k!='layers'}
    header['layer_count']=len(row['layers'])
    lines=[line('DUO_PROOF',header)]
    for index,sha,x,y in row['layers']:
        lines.append(line('DUO_PROOF_LAYER',dict(rev=U64,scene_revision=U64,index=index,hash=sha,x=x,y=y)))
    return row,lines


def status_lines():
    row=dict(generation=U32,toggles_sent=U32,retransmitted=U32,resyncs=U32,answered=U32,displayed=U32,
        taps_ignored_pending=U32,taps_injected=U32,taps_physical=U32,character='angel',rev=U64,
        baseline=True,pending=False,internal_heap_free=U32,internal_heap_minimum=U32)
    lines=[line('DUO_STATUS',row)]
    for part in range(2):
        lines.append(line('DUO_ERRORS',dict(generation=U32,rev=U64,part=part,parts=2,
            errors={k:U32 for k in FAULTS[part*8:(part+1)*8]})))
    return dict(row,errors={k:U32 for k in FAULTS}),lines


def test_worst_widths_reassemble_every_field_and_keep_fifteen_percent_margin():
    p,pl=proof_lines();s,sl=status_lines()
    assert all(len(('\n'+text+'\n').encode())<=420 and len(('\n'+text+'\n').encode())*100<=496*85 for text in pl+sl)
    got=smoke.records(pl,'DUO_PROOF')[0]
    assert got==dict(p,layer_count=3)
    assert smoke.proof_matches(got,state(p))==got
    assert smoke.records(sl,'DUO_STATUS')==[s]
    status_matches(s,got)  # The touch adapter uses this same reconstructed shape.


@pytest.mark.parametrize('kind',['proof','status'])
def test_each_truncated_line_is_rejected_without_partial_results(kind):
    _,lines=proof_lines() if kind=='proof' else status_lines()
    prefix='DUO_PROOF' if kind=='proof' else 'DUO_STATUS'
    for index in range(len(lines)):
        damaged=lines.copy();damaged[index]=damaged[index][:-5]
        with pytest.raises(ValueError):smoke.records(damaged,prefix)


@pytest.mark.parametrize('missing',[1,2,3])
def test_missing_layer_is_never_replaced_by_a_previous_complete_proof(missing):
    _,lines=proof_lines();incomplete=lines[:missing]+lines[missing+1:]
    with pytest.raises(smoke.IncompleteRecord):smoke.records(incomplete,'DUO_PROOF')
    with pytest.raises(smoke.IncompleteRecord):smoke.records(lines+incomplete,'DUO_PROOF')


@pytest.mark.parametrize('key',['rev','scene_revision'])
def test_foreign_layer_binding_rejected(key):
    _,lines=proof_lines();bad=json.loads(lines[2].split(' ',1)[1]);bad[key]-=1
    lines[2]=line('DUO_PROOF_LAYER',bad)
    with pytest.raises(ValueError,match='revision/scene'):smoke.records(lines,'DUO_PROOF')


@pytest.mark.parametrize('bad',['duplicate','orphan','missing-error','error-revision','error-generation','missing-counter'])
def test_invalid_sets_fail_closed(bad):
    _,lines=proof_lines();prefix='DUO_PROOF'
    if bad=='duplicate':lines.insert(2,lines[1])
    if bad=='orphan':lines=lines[1:]
    if bad.startswith('error-') or bad in ('missing-error','missing-counter'):
        _,lines=status_lines();prefix='DUO_STATUS'
        if bad=='missing-error':lines.pop()
        else:
            row=json.loads(lines[-1].split(' ',1)[1])
            if bad=='error-revision':row['rev']-=1
            elif bad=='error-generation':row['generation']-=1
            else:row['errors'].pop('other')
            lines[-1]=line('DUO_ERRORS',row)
    with pytest.raises(ValueError):smoke.records(lines,prefix)


def test_live_snapshot_and_proof_timestamp_wait_for_the_last_layer():
    p,pl=proof_lines();s,sl=status_lines()
    class Board:
        def __init__(self):self.rows=[(i+1,10+i,text) for i,text in enumerate(pl)];self.cursor=len(self.rows)
        def command(self,text,timeout):
            assert text=='duo status'
            for record in sl:
                self.cursor+=1;self.rows.append((self.cursor,10+self.cursor,record))
        def lines_since(self,cursor):return [row for row in self.rows if row[0]>cursor]
    io=smoke.LiveIO.__new__(smoke.LiveIO);io.board=Board();io.alive=lambda:None
    io.lines=lambda:[row[2] for row in io.board.rows]
    status,shown=io.snapshot()
    assert status==s and shown==dict(p,layer_count=3)
    assert io.proof_received_at(shown)==13_000_000_000
    assert io.complete_records(pl[:-1],'DUO_PROOF') is None


def test_raw_serial_file_tail_can_stop_inside_json_until_the_next_chunk(tmp_path):
    _,lines=proof_lines();path=tmp_path/'serial.log'
    io=smoke.LiveIO.__new__(smoke.LiveIO);io.out=tmp_path
    payload=('\n'.join(lines)+'\n').encode()
    for boundary in range(1,len(payload)):
        path.write_bytes(payload[:boundary])
        complete=io.lines()
        assert all(text.endswith('}') for text in complete)
        # Partial headers are unscored; complete headers wait for all layers.
        assert io.complete_records(complete,'DUO_PROOF') in ([],None)
    path.write_bytes(payload)
    assert len(io.complete_records(io.lines(),'DUO_PROOF'))==1
    # A truncated JSON record that HAS a newline is a real error, not a tail.
    path.write_bytes(payload[:-10]+b'\n')
    with pytest.raises(ValueError):io.complete_records(io.lines(),'DUO_PROOF')
