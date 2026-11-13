"""Per-campaign, board and part attempt cap; retained, sealed result summaries."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path

from atomic_json import atomic_write_json
from duo_identity import digest,require


@contextmanager
def ledger(out,board,part):
    out=Path(out).resolve();parent=out.parent
    require(parent.name==part and parent.parent.name==board,'use CAMPAIGN/BOARD/PART/attempt-01 or attempt-02')
    require(out.name in ('attempt-01','attempt-02'),'at most two numbered attempts')
    parent.mkdir(parents=True,exist_ok=True)
    with (parent/'attempts.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=parent/'attempts.json'
        rows=json.loads(path.read_text()) if path.exists() else []
        require(len(rows)<=2,'attempt cap exceeded')
        for number,row in enumerate(rows,1):
            expected=parent/f'attempt-{number:02d}'
            require(row['out']==str(expected) and expected.is_dir(),'registered attempt missing/changed')
            if row.get('result_sha256'):
                require(digest(expected/'result.json')==row['result_sha256'],'retained result changed')
        require({p.name for p in parent.glob('attempt-*')}=={Path(r['out']).name for r in rows},'unregistered attempt present')
        yield path,rows


def register(out,board,part):
    out=Path(out).resolve()
    with ledger(out,board,part) as (path,rows):
        number=len(rows)+1
        require(number<=2 and out.name==f'attempt-{number:02d}' and not out.exists(),'attempt order/cap differs')
        if rows:
            prior=rows[-1]
            require(prior.get('result_sha256') and prior.get('retryable') is True and prior.get('passed') is False,
                    'only a clean, proven missed injection window earns one retry')
        out.mkdir()
        rows.append(dict(out=str(out),board=board,part=part,attempt=number,status='started'))
        atomic_write_json(path,rows)
        return dict(attempt=number,prior_attempts=rows[:-1],max_attempts=2)


def finish(out,board,part,result):
    out=Path(out).resolve()
    with ledger(out,board,part) as (path,rows):
        require(rows and rows[-1]['out']==str(out) and rows[-1]['status']=='started','attempt not open')
        rows[-1].update(status='finished',passed=result['passed'],retryable=result.get('retryable',False),
                        error=result.get('error'),tap_latencies=result.get('tap_latencies',[]),result_sha256=digest(out/'result.json'))
        atomic_write_json(path,rows)
        atomic_write_json(out.parent/'attempts-summary.json',dict(board=board,part=part,max_attempts=2,attempts=rows))
