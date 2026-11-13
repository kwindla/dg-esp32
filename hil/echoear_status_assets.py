"""Standalone status provisioning helpers; caller owns bench and idle board.

No services, board resolution or hardware access occurs on import. Worker A's
launcher may call provision_status() after cat setup, while its asset origin is
serving hil/fixtures/echoear360; boot uses the same hashes from demo_assets.h.
"""
import json
from pathlib import Path
from step9a_support import request,terminal
ROOT=Path(__file__).resolve().parents[1]


def status_assets():
    return json.loads((ROOT/'hil/fixtures/echoear360/status.json').read_text())['assets']


def _finish(board,kind,data,request_id):
    posted=request(board,kind,data,request_id)
    result=terminal(board,posted['id'])
    if result['error']!='none':
        raise RuntimeError('Demo status '+kind+' failed: '+result['error'])
    return result


def provision_status(board,origin):
    """Persist all five status assets. Must run before connect/while idle."""
    results=[]
    for row in status_assets():
        data=dict(hash=row['hash'],bytes=row['bytes'],url=origin.rstrip('/')+'/'+row['hash']+'.dgi')
        _finish(board,'load',data,'status-load-'+str(row['slot']))
        result=_finish(board,'persist',dict(hash=row['hash']),'status-persist-'+str(row['slot']))
        if not result.get('persisted'):
            raise RuntimeError('Demo status persistence was not confirmed')
        results.append(result)
    return results


def preload_status(board):
    """Load persisted assets with no fixture server/network dependency."""
    return [_finish(board,'preload',dict(hash=row['hash']),'status-preload-'+str(row['slot']))
            for row in status_assets()]
