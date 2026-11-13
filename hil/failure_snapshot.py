"""Best-effort device evidence before teardown, preserving the original failure."""
import json
import time
from pathlib import Path


def capture_failure(board, out, reason):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    cursor=board.cursor
    result=dict(reason=str(reason),observed_monotonic_s=time.monotonic(),commands=[])
    # netprobe-stats is safe when inactive and reports its active state.
    for command in ('perf','wifi-stats','netprobe stats'):
        try:
            ack=board.command(command,timeout=12)
            result['commands'].append(dict(command=command,ack=ack))
        except Exception as exc:
            result['commands'].append(dict(command=command,error=f'{type(exc).__name__}: {exc}'))
    try:
        result['lines']=board.lines_since(cursor)
    except Exception as exc: result['read_error']=str(exc)
    (out/'failure-device-snapshot.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
