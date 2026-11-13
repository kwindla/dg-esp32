#!/usr/bin/env python3
"""Compile instrumented copies using frozen build flags; never edit production/SDK."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import os

ROOT=Path('/home/khkramer/src/dg-esp32')
BASE=ROOT/'build'
OUT=BASE/'r4b-profile'
OVERLAY=OUT/'overlay'
EVIDENCE=Path('/home/khkramer/src/evidence/step9a/r4b')
PROFILE=ROOT/'diagnostics/r4b'


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def edit(name,fn):
    s=(ROOT/name).read_text();updated=fn(s)
    assert updated!=s,name
    p=OVERLAY/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(updated)
    return name
def replace(s,a,b):
    assert a in s,a[:100]
    return s.replace(a,b)
def scope(s,signature,stage):
    return replace(s,signature,signature+' R4B_SCOPE('+stage+');')
def include(s):return '#include "profile.h"\n'+s


def main():
    OUT.mkdir(exist_ok=True);OVERLAY.mkdir(exist_ok=True)
    changed=[]
    changed.append(edit('main/main.cpp',lambda s:replace(include(s),'dg_diagnostics_init(); log_heap("boot")','dg_diagnostics_init(); r4b_init(); log_heap("boot")')))
    changed.append(edit('main/console.cpp',lambda s:replace(include(s),'if (!std::strcmp(command, "ping")) {}','if (!std::strcmp(command, "ping")) {}\n                    else if (!std::strcmp(command,"profile")) r4b_command(std::strstr(line,"profile")+7);')))
    def app(s):
        s=include(s);s=scope(s,'void post(const Event& e) {','R4bAppQueue');return scope(s,'void handle_event(const Event& e) {','R4bAppHandle')
    changed.append(edit('main/app.cpp',app))
    changed.append(edit('third_party/pipecat-client-cxx/src/client.cpp',lambda s:scope(scope(include(s),'void PipecatClient::Impl::on_transport_message(const json& raw) {','R4bRtviConvert'),'void PipecatClient::Impl::handle_message(const rtvi::Message& message) {','R4bRtviDispatch')))
    changed.append(edit('third_party/pipecat-client-cxx/src/event_loop.cpp',lambda s:replace(scope(include(s),'void EventLoop::post(std::function<void()> task) {','R4bClientQueue'),'            task();','            R4B_SCOPE(R4bClientCallback);task();')))
    def transport(s):
        s=include(s);s=replace(s,'const auto wire=std::string_view(reinterpret_cast<const char*>(frame->data),size_t(frame->size));','r4b_frame(size_t(frame->size));\n            const auto wire=std::string_view(reinterpret_cast<const char*>(frame->data),size_t(frame->size));')
        s=replace(s,'auto message = nlohmann::json::parse(wire.data, wire.data + size, nullptr, false);','auto message = [&]{R4B_SCOPE(R4bJsonParse);return nlohmann::json::parse(wire.data, wire.data + size, nullptr, false);}();')
        return replace(s,'observer->on_transport_message(message);','observer->on_transport_message(message);r4b_frame(size,true);')
    changed.append(edit('components/pipecat_esp_peer/src/transport.cpp',transport))
    def queue(s):
        s=scope(include(s),'MessageKind classify_message(std::string_view text, size_t max_depth) {','R4bClassifier')
        s=scope(s,'MessageQueue::Result MessageQueue::push_classified(std::string_view message, int64_t now_ms, MessageKind kind) {','R4bNativeQueue')
        return scope(s,'bool MessageQueue::pop(char* output, size_t bytes, size_t& size, int64_t& arrival) {','R4bNativeQueue')
    changed.append(edit('components/pipecat_esp_peer/src/msg_queue.cpp',queue))
    edit('components/pipecat_esp_peer/src/peer_trace.h',lambda s:replace(include(s),'PeerTrace* trace_;','R4bToken r4b_profile_{};\n    PeerTrace* trace_;'))
    def trace(s):
        s=include(s);s=replace(s,'PeerTimer::PeerTimer(PeerPart part):trace_(active_peer_trace),part_(part),cycles_(0),time_(0) {','PeerTimer::PeerTimer(PeerPart part):trace_(active_peer_trace),part_(part),cycles_(0),time_(0) {\n    unsigned stage=part==PeerPart::Main?R4bPeerMain:part==PeerPart::DtlsRead?R4bDtlsRead:part==PeerPart::DataCallback?R4bDataCallback:part==PeerPart::Recv?R4bSocketRecv:part==PeerPart::Send?R4bSocketSend:99;\n    if(stage!=99)r4b_profile_=r4b_enter(stage);')
        return replace(s,'PeerTimer::~PeerTimer() {','PeerTimer::~PeerTimer() {r4b_leave(r4b_profile_);')
    changed.append(edit('components/pipecat_esp_peer/src/peer_trace.cpp',trace))
    def diagnostics(s):
        s=scope(include(s),'static int deferred_logv(const char* format, va_list args) {','R4bLogEnqueue')
        return replace(s,'for (;;) if(xQueueReceive(log_queue,line,portMAX_DELAY)==pdTRUE) {','for (;;) if(xQueueReceive(log_queue,line,portMAX_DELAY)==pdTRUE) {R4B_SCOPE(R4bLogSink);')
    changed.append(edit('components/dg_diagnostics/diagnostics.cpp',diagnostics))
    edit('components/dg_display/include/dg_display/runtime.h',lambda s:replace(s,'void runtime_dma_dead() noexcept;','void runtime_dma_dead(unsigned site=15,uint64_t wait=0) noexcept;'))
    def runtime(s):
        s=include(s)
        s=replace(s,'void lock(){if(xSemaphoreTake(state->lock,pdMS_TO_TICKS(2000))!=pdTRUE){state->dead=true;state->failed=true;','void lock(){auto r4b_began=esp_timer_get_time();if(xSemaphoreTake(state->lock,pdMS_TO_TICKS(2000))!=pdTRUE){r4b_dead(1,esp_timer_get_time()-r4b_began,uintptr_t(__builtin_return_address(0)));state->dead=true;state->failed=true;')
        s=replace(s,'void dead_display(){state->dead=true;state->failed=true;}','void dead_display(unsigned site=2,uint64_t wait=0){r4b_dead(site,wait,uintptr_t(__builtin_return_address(0)));state->dead=true;state->failed=true;}')
        s=replace(s,'if((began && now-began>2000000) || (state->color_inflight.load() && stripe && now-stripe>2000000))dead_display();','if((began && now-began>2000000) || (state->color_inflight.load() && stripe && now-stripe>2000000))dead_display(began && now-began>2000000?3:4,began && now-began>2000000?now-began:now-stripe);')
        s=replace(s,'bool await_dma(){\n    if(!ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(2000))){dead_display();return false;}','bool await_dma(){auto r4b_began=esp_timer_get_time();\n    if(!ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(2000))){dead_display(5,esp_timer_get_time()-r4b_began);return false;}')
        s=replace(s,'auto error=esp_lcd_panel_io_tx_param(state->io,-1,nullptr,0);','auto r4b_began=esp_timer_get_time();auto error=esp_lcd_panel_io_tx_param(state->io,-1,nullptr,0);')
        s=replace(s,'if(error!=ESP_OK){dead_display();return false;}','if(error!=ESP_OK){dead_display(6,esp_timer_get_time()-r4b_began);return false;}')
        s=replace(s,'if(!acquired){++state->display_gate_timeouts;dead_display();return false;}','if(!acquired){++state->display_gate_timeouts;dead_display(7,esp_timer_get_time()-requested);return false;}')
        s=replace(s,'if(admitted.admission!=Admission::Accepted){state->failed=true;','if(admitted.admission!=Admission::Accepted){r4b_dead(8,0,uintptr_t(__builtin_return_address(0)));state->failed=true;')
        s=replace(s,'{state->failed=true;break;}y+=rows;','{r4b_dead(9,esp_timer_get_time()-t.begin,uintptr_t(__builtin_return_address(0)));state->failed=true;break;}y+=rows;')
        s=replace(s,'if(!drain_dma() || dma_generation()!=generation)state->failed=true;','if(!drain_dma() || dma_generation()!=generation){r4b_dead(10,esp_timer_get_time()-t.begin,uintptr_t(__builtin_return_address(0)));state->failed=true;}')
        s=replace(s,'void runtime_dma_dead() noexcept{if(state)dead_display();}','void runtime_dma_dead(unsigned site,uint64_t wait) noexcept{if(state)dead_display(site,wait);}')
        s=replace(s,'if(dma_bounces())dead_display();','if(dma_bounces())dead_display(11,0);')
        # Same-core atomic/short protected reads; never take the engine semaphore
        # from the high-priority sampler-stop task.
        marker='RuntimeHealth runtime_health() noexcept'
        snapshot='''extern "C" void r4b_display_snapshot(uint64_t out[13]) noexcept{for(unsigned i=0;i<13;++i)out[i]=0;out[0]=esp_timer_get_time();if(!state)return;static portMUX_TYPE read_mux=portMUX_INITIALIZER_UNLOCKED;portENTER_CRITICAL(&read_mux);out[1]=state->engine.rendered;out[2]=state->stripes_completed.load();out[3]=state->stripes_attempted.load();out[4]=state->dma_active_us.load();out[5]=state->scheduling_gap_max_us.load();out[6]=dma_errors();out[7]=state->dead.load();out[8]=state->failed.load();out[9]=state->send_failures.load();out[10]=state->late_callbacks.load();out[11]=state->display_gate_timeouts.load();out[12]=state->spi_coverage_errors.load();portEXIT_CRITICAL(&read_mux);}\n'''
        return replace(s,marker,snapshot+marker)
    changed.append(edit('components/dg_display/runtime.cpp',runtime))
    def bringup(s):
        s=include(s);s=replace(s,'auto error=__real_spi_device_get_trans_result(device,result,device==lcd_device?std::min(wait,LcdWait):wait);','auto r4b_began=esp_timer_get_time();auto error=__real_spi_device_get_trans_result(device,result,device==lcd_device?std::min(wait,LcdWait):wait);')
        s=replace(s,'auto error=__real_spi_device_queue_trans(device,transaction,device==lcd_device?std::min(wait,LcdWait):wait);','auto r4b_began=esp_timer_get_time();auto error=__real_spi_device_queue_trans(device,transaction,device==lcd_device?std::min(wait,LcdWait):wait);')
        old='if(device==lcd_device && error==ESP_ERR_TIMEOUT)dg_display::runtime_dma_dead();'
        assert s.count(old)==2
        s=s.replace(old,'if(device==lcd_device && error==ESP_ERR_TIMEOUT)dg_display::runtime_dma_dead(12,esp_timer_get_time()-r4b_began);',1)
        s=s.replace(old,'if(device==lcd_device && error==ESP_ERR_TIMEOUT)dg_display::runtime_dma_dead(13,esp_timer_get_time()-r4b_began);',1)
        s=replace(s,'error=spi_device_polling_end(device,LcdWait);','auto r4b_began=esp_timer_get_time();error=spi_device_polling_end(device,LcdWait);')
        return replace(s,'dg_display::runtime_dma_dead();','dg_display::runtime_dma_dead(14,esp_timer_get_time()-r4b_began);')
    changed.append(edit('components/dg_display/bringup.cpp',bringup))
    def crypto(s):
        s=include(s);s=replace(s,'auto& p=paths[index];++p.calls;unsigned traversed=0;','auto& p=paths[index];++p.calls;unsigned traversed=0;uint64_t r4b_psram=0,r4b_internal=0;')
        s=replace(s,'const bool external=esp_ptr_external_ram(d->buffer);','const bool external=esp_ptr_external_ram(d->buffer);if(external)r4b_psram+=count;else r4b_internal+=count;')
        s=replace(s,'return index;\n}','r4b_crypto_transfer(c.peripheral,c.tx,r4b_psram,r4b_internal);return index;\n}')
        s=replace(s,'uint8_t* stream){const int token=','uint8_t* stream){R4B_SCOPE(R4bAes);const int token=')
        return replace(s,'size_t alen){const bool psram_aad=','size_t alen){R4B_SCOPE(R4bAes);const bool psram_aad=')
    changed.append(edit('components/dg_display/r4_crypto.cpp',crypto))
    def hash_code(s):
        s=include(s)
        # Insert scopes at the wrapper entry, with signatures checked below.
        import re
        for symbol,stage in [('__wrap_esp_sha_dma','R4bSha'),('__wrap_esp_crypto_shared_gdma_start_axi_ahb','R4bCryptoSetup')]:
            pattern=r'('+symbol+r'\([^;{}]*\)\s*\{)';s,n=re.subn(pattern,r'\1 R4B_SCOPE('+stage+');',s,count=1);assert n==1,symbol
        return s
    changed.append(edit('components/dg_display/r5_hash.cpp',hash_code))

    commands=json.loads((BASE/'compile_commands.json').read_text())
    archives={};sources={}
    for name in changed:
        row=next(r for r in commands if r['file']==str(ROOT/name))
        argv=shlex.split(row['command']);obj=OUT/'objects'/Path(row['output']).name;obj.parent.mkdir(exist_ok=True)
        argv[argv.index('-o')+1]=str(obj);argv[argv.index('-c')+1]=str(OVERLAY/name)
        argv[1:1]=['-I'+str(PROFILE),'-I'+str(OVERLAY/'components/pipecat_esp_peer/src'),'-I'+str(OVERLAY/'components/dg_display/include'),'-I'+str(ROOT/'components/dg_display/include/dg_display')]
        component=row['output'].split('/')[1]
        archive=OUT/'lib'/('lib'+component+'.a');archive.parent.mkdir(exist_ok=True)
        if component not in archives:shutil.copy2(BASE/'esp-idf'/component/('lib'+component+'.a'),archive);archives[component]=archive
        print('COMPILE '+name,flush=True);subprocess.run(argv,cwd=BASE,check=True)
        ar=Path(argv[0]).with_name('xtensa-esp32s3-elf-ar');subprocess.run([str(ar),'r',str(archive),str(obj)],check=True,cwd=BASE)
        sources[name]={'production_sha256':sha(ROOT/name),'overlay_sha256':sha(OVERLAY/name)}
    row=next(r for r in commands if r['file'].endswith('/main/console.cpp'));argv=shlex.split(row['command']);obj=OUT/'objects/profile.cpp.obj'
    argv[argv.index('-o')+1]=str(obj);argv[argv.index('-c')+1]=str(PROFILE/'profile.cpp')
    argv[1:1]=['-I'+str(PROFILE),'-I/home/khkramer/esp/esp-idf-v6.1/components/mbedtls/mbedtls/library',
        '-I/home/khkramer/esp/esp-idf-v6.1/components/esp_mm/include',
        '-I/home/khkramer/esp/esp-idf-v6.1/components/esp_hal_timg/include',
        '-I/home/khkramer/esp/esp-idf-v6.1/components/esp_hal_timg/esp32s3/include']
    print('COMPILE diagnostic profile.cpp',flush=True);subprocess.run(argv,cwd=BASE,check=True)
    ar=Path(argv[0]).with_name('xtensa-esp32s3-elf-ar');subprocess.run([str(ar),'r',str(archives['main']),str(obj)],check=True)
    link=subprocess.check_output(['ninja','-C',str(BASE),'-t','commands','dg-esp32.elf'],text=True).splitlines()[-1]
    argv=shlex.split(link);assert argv[:2]==[':','&&'] and argv[-2:]==['&&',':'];argv=argv[2:-2]
    for i,arg in enumerate(argv):
        for component,archive in archives.items():
            if arg==f'esp-idf/{component}/lib{component}.a':argv[i]=str(archive)
        if arg.startswith('-Wl,--Map='):argv[i]='-Wl,--Map='+str(OUT/'dg-esp32.map')
    argv[argv.index('-o')+1]=str(OUT/'dg-esp32.elf')
    wraps=['heap_caps_malloc_base','heap_caps_calloc_base','heap_caps_realloc_base','heap_caps_aligned_alloc_base','heap_caps_free','malloc','calloc','realloc','free','_Znwj','_Znaj','mbedtls_ssl_decrypt_buf','mbedtls_ssl_read','sctp_incoming_data','aes_hal_wait_done','sha_hal_wait_idle','esp_cache_msync']
    argv += ['-Wl,--wrap='+s for s in wraps]
    print('LINK diagnostic-only copies',flush=True);subprocess.run(argv,cwd=BASE,check=True)
    python='/home/khkramer/.espressif/python_env/idf6.1_py3.12_env/bin/python'
    subprocess.run([python,'-m','esptool','--chip','esp32s3','elf2image','--flash-mode','dio','--flash-freq','80m','--flash-size','16MB','--elf-sha256-offset','0xb0','--flash-mmu-page-size','64KB','--min-rev-full','0','--max-rev-full','99','-o',str(OUT/'dg-esp32.bin'),str(OUT/'dg-esp32.elf')],cwd=BASE,check=True)
    image_name=os.environ.get('STEP9A_R4B_PROFILE_IMAGE_NAME','image-profile')
    snapshot=EVIDENCE/image_name;snapshot.mkdir(exist_ok=False)
    for name in ['dg-esp32.elf','dg-esp32.bin','dg-esp32.map']:shutil.copy2(OUT/name,snapshot/name)
    source_snapshot=EVIDENCE/('source-'+image_name)
    for name in changed+['components/pipecat_esp_peer/src/peer_trace.h','components/dg_display/include/dg_display/runtime.h']:
        dst=source_snapshot/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(OVERLAY/name,dst)
    for name in ['profile.cpp','profile.h']:
        dst=source_snapshot/'diagnostics/r4b'/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(PROFILE/name,dst)
    metadata={'image':{n:sha(snapshot/n) for n in ['dg-esp32.elf','dg-esp32.bin','dg-esp32.map']},'production_base_elf':sha(EVIDENCE/'image-production/dg-esp32.elf'),'production_sources_untouched':sources,'wrapper_symbols':wraps,'method':'instrumented source copies + separate copied component archives, original compile/link flags and SDK objects; no config/SDK/production source edits','sources':{str(p.relative_to(source_snapshot)):sha(p) for p in source_snapshot.rglob('*') if p.is_file()}}
    (EVIDENCE/(image_name+'-freeze.json')).write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps({'diagnostic_ELF':metadata['image']['dg-esp32.elf'],'frozen':str(snapshot)}),flush=True)


if __name__=='__main__':main()
