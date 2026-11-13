"""Strict decoder for post-acquisition buffered WiFi observations.

No serial/network activity on import. Completion callbacks are not air
timestamps, retries, ACK latency, per-RTP counters or channel occupancy.
"""
import json
import re


def decode(lines):
    extent=[s for s in lines if s.startswith('WIFI_OBSERVE ')]
    if (len(extent)<2 or not extent[0].startswith('WIFI_OBSERVE BEGIN ') or
            not extent[-1].startswith('WIFI_OBSERVE END ') or
            any(not s.startswith('WIFI_OBSERVE ROW ') for s in extent[1:-1])):
        raise ValueError('One ordered BEGIN/ROW/END observation extent required')
    meta=json.loads(extent[0].removeprefix('WIFI_OBSERVE BEGIN '))
    if type(meta.get('schema')) is not int or meta['schema']!=1 or type(meta.get('enabled')) is not bool:raise ValueError('Unknown observation schema')
    rows=[json.loads(s.removeprefix('WIFI_OBSERVE ROW ')) for s in extent[1:-1]]
    number=lambda x:type(x) is int and x>=0
    fields=('samples','retained','overwritten','first_sequence','last_sequence','capacity')
    if any(not number(meta.get(k)) for k in fields):raise ValueError('Invalid extent metadata')
    if len(rows)!=meta['retained'] or extent[-1]!=f"WIFI_OBSERVE END last_sequence={meta['last_sequence']}":
        raise ValueError('Truncated/duplicated observation export')
    if (meta['capacity'] not in (0,256) or meta['retained']!=min(meta['samples'],meta['capacity']) or
            meta['overwritten']!=meta['samples']-meta['retained'] or
            meta['last_sequence']!=meta['samples']):raise ValueError('Inconsistent observation extent')
    if meta['retained'] and meta['first_sequence']!=meta['last_sequence']-len(rows)+1:
        raise ValueError('Invalid sequence bounds')
    if not meta['enabled'] and (rows or meta['samples']):raise ValueError('Disabled observer cannot have records')
    if type(meta.get('startup_error')) is not int:raise ValueError('Invalid startup error')
    if (any(type(meta.get(k)) is not bool for k in ('tx_done_registered','tx_done_available')) or
            meta['tx_done_available']!=(meta['tx_done_registered'] and meta['startup_error']==0)):
        raise ValueError('Invalid completion capability flags')
    if ((not meta['enabled'] and (meta['tx_done_registered'] or meta['startup_error'] or meta['capacity'])) or
            (meta['enabled'] and meta['capacity']!=256) or (rows and not meta['tx_done_available'])):
        raise ValueError('Unavailable observer cannot produce completion records')
    if any(k not in meta or meta[k] is not None for k in ('tx_retries','send_to_air_us','channel_occupancy')):
        raise ValueError('Unsupported metric cannot become a fabricated value')
    last_end=None
    for sequence,row in enumerate(rows,meta['first_sequence']):
        if any(k not in row for k in ('ps_mode','rssi_dbm','primary_channel','secondary_channel','bssid')):
            raise ValueError('Explicit nullable readback fields required')
        if type(row.get('sequence')) is not int or row['sequence']!=sequence:raise ValueError('Missing/reordered/duplicate observation')
        clocks=[row.get(k) for k in ('counter_begin_us','sample_begin_us','sample_end_us')]
        if not all(number(x) for x in clocks) or not clocks[0]<=clocks[1]<=clocks[2]:
            raise ValueError('Invalid observation timestamps')
        if last_end is not None and (clocks[0]!=last_end or clocks[1]<last_end):
            raise ValueError('Noncontinuous sample intervals')
        last_end=clocks[2]
        if (any(type(row.get(k)) is not int for k in ('ps_error','ap_error')) or
                any(not number(row.get(k)) or row[k]>0xffffffff for k in ('tx_done_success','tx_done_failed'))):
            raise ValueError('Invalid API errors/completion counters')
        if row['ps_error']==0:
            if type(row.get('ps_mode')) is not int or row['ps_mode'] not in (0,1,2):raise ValueError('Invalid PS enum')
        elif row.get('ps_mode') is not None:raise ValueError('Failed PS readback must be null')
        apfields=('rssi_dbm','primary_channel','secondary_channel','bssid')
        if row['ap_error']:
            if any(row.get(k) is not None for k in apfields):raise ValueError('Failed AP readback must be null')
        elif (type(row.get('rssi_dbm')) is not int or type(row.get('primary_channel')) is not int or
              not 1<=row['primary_channel']<=14 or type(row.get('secondary_channel')) is not int or
              not isinstance(row.get('bssid'),str) or not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',row['bssid'])):
            raise ValueError('Invalid AP record')
    return dict(metadata=meta,rows=rows,available=bool(meta['enabled'] and meta.get('startup_error')==0 and rows),
        callback_activity_observed=any(r['tx_done_success'] or r['tx_done_failed'] for r in rows),
        retained_history_not_overwritten=meta['overwritten']==0,
        qualification=False,basis='Native completion statuses only; unavailable retries/airtime remain null')


def dump(board,path):
    """Caller already owns bench/serial. Call only AFTER acquisition stops."""
    cursor=board.cursor
    board.command('wifi-observe',12)
    result=decode([line for _,_,line in board.lines_since(cursor)])
    path.write_text(json.dumps(result,indent=2)+'\n')
    return result
