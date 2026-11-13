"""Read-only retained-R2 attribution; reconstructed JSON is diagnostic only."""
import json,hashlib
from pathlib import Path
BASE=Path('/home/khkramer/src/evidence/step9a/r2')
OUT=Path('/home/khkramer/src/evidence/step9a/r3')

def main():
 p=BASE/'campaign-01/fetch-B1/serial.log';raw=p.read_bytes();lines=raw.splitlines();error=b'E spi_master: DMA TX underflow detected';at=next(i for i,l in enumerate(lines) if error in l)
 reconstruction=lines[at].replace(error,b'')+lines[at+1];perf=json.loads(reconstruction.split(b'PERF ',1)[1])
 cleanup=json.loads((BASE/'production-stop-cleanup/result.json').read_text());record=cleanup['dma_trace']['items'][0];works=[];states=[]
 for i,line in enumerate(lines,1):
  if b'DISPLAY_WORK_RESULT ' in line:works.append({'line':i,'text':line.decode(),'value':json.loads(line.split(b'DISPLAY_WORK_RESULT ',1)[1])})
  if b'STATE Idle' in line or b'tx-exit t=' in line or b'agent disconnected done' in line or (line.startswith(b'GATE ') and i>at-30):states.append({'line':i,'text':line.decode()})
 end=perf['heap_window']['end_us'];tail=cleanup['flash_trace']['items'];result={'diagnostic_analysis_only':True,'raw_sha256':hashlib.sha256(raw).hexdigest(),'raw_unchanged':True,'failed_boot_remains_failed':True,'error_line':at+1,'reconstruction':'remove exactly one driver message, join immediately following PERF continuation; never used for passing scores','perf_device_us':perf['device_us'],'perf_heap_window_end_us':end,'stripe_start_us':record[1],'callback_us':record[2],'recycle_us':record[3],'start_after_heap_snapshot_us':record[1]-end,'callback_after_start_us':record[2]-record[1],'recycle_after_callback_us':record[3]-record[2],'hooked_read_prog_erase_overlap':record[5:8],'works':works,'state_serial_order':states,'retained_flash_tail':{'oldest':cleanup['flash_trace']['oldest'],'total':cleanup['flash_trace']['total'],'first_start_us':tail[0][3],'last_end_us':tail[-1][4]},'excluded':'An overlapping instrumented LittleFS block read/prog/erase was not recorded in this stripe window. In-call load terminated RAM-only before teardown.','not_excluded':['unhooked SPI1 ops: r2 had no global API instrumentation','XIP instruction misses and PSRAM data traffic','LittleFS CPU work between block operations','PERF heap walks/JSON allocation/USB output','session teardown and auto-persist validation'],'not_identified':['auto-persist start time','frame kind/rectangle of the failed stripe: original frame ring rolled out','precise hardware starvation instant or MSPI requester'],'serial_order_limit':'Serialized output/interleaved buffers are not a CPU execution trace. Explicit device timestamps take precedence over line order; ROM driver logging bypasses the console TX path.'}
 (OUT/'step1-attribution.json').write_text(json.dumps(result,indent=2)+'\n')
 (OUT/'fetch-B1-perf-reconstruction-diagnostic.json').write_text(json.dumps(perf,indent=2)+'\n')
 first=json.loads((BASE/'diagnose-01/failure-followup/result.json').read_text())['dma_trace']['items'][0]
 print({'fetch_B1':{k:result[k] for k in ('perf_device_us','perf_heap_window_end_us','stripe_start_us','callback_us','recycle_us','start_after_heap_snapshot_us')},'work_count':len(works),'diagnose01_first_error':first,'flash_tail':result['retained_flash_tail']})

if __name__=='__main__':main()
