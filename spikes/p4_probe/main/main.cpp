// P4 audio probe: production-equivalent I2S/codec setup ported from
// components/dg_audio/src/echoear_{i2s,codec}.cpp (AEC profile: RX stereo32 TDM
// carrying MIC1..MIC4, TX data16/slot32), with Waveshare P4-XC pins.
// Console (UART0, 115200): tone / rec / dump / duplex / gain.
#include "console.h"
#include "audio_b.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mbedtls/base64.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {
constexpr int kRate = 16000;
constexpr gpio_num_t kSda = GPIO_NUM_7, kScl = GPIO_NUM_8, kPa = GPIO_NUM_53;
constexpr gpio_num_t kMclk = GPIO_NUM_13, kBclk = GPIO_NUM_12, kWs = GPIO_NUM_10, kDout = GPIO_NUM_9, kDin = GPIO_NUM_11;

i2c_master_bus_handle_t bus;
i2s_chan_handle_t rx, tx;
const audio_codec_if_t *dac, *adc;
bool audio_ready=false;

// Initialization errors must leave UART recovery/diagnostics usable.
#define INIT_OK(call) do { int rc=(call); if(rc!=ESP_OK) { \
    probe::print("CODEC_INIT_FAILED rc=%d operation=%s\n",rc,#call); return false; } } while(0)

bool init_i2s(unsigned descriptors=6,unsigned frames=320) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = descriptors; cc.dma_frame_num = frames; cc.auto_clear = true;
    INIT_OK(i2s_new_channel(&cc, &tx, &rx));
    i2s_std_config_t sc{};
    sc.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(kRate);
    sc.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO);
    sc.slot_cfg.slot_bit_width = I2S_SLOT_BIT_WIDTH_32BIT; sc.slot_cfg.ws_width = 32;
    sc.gpio_cfg.mclk = kMclk; sc.gpio_cfg.bclk = kBclk; sc.gpio_cfg.ws = kWs;
    sc.gpio_cfg.dout = kDout; sc.gpio_cfg.din = kDin;
    INIT_OK(i2s_channel_init_std_mode(rx, &sc));
    // As on EchoEar: RX owns the external 256fs MCLK; TX internal 512fs, data16 in slot32.
    sc.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_512;
    sc.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    sc.slot_cfg.data_bit_width = I2S_DATA_BIT_WIDTH_16BIT;
    INIT_OK(i2s_channel_init_std_mode(tx, &sc));
    i2s_chan_info_t ri{}, ti{};
    INIT_OK(i2s_channel_get_info(rx, &ri));
    INIT_OK(i2s_channel_get_info(tx, &ti));
    probe::print("I2S rx role=%d mclk=%lu bclk=%lu tx role=%d mclk=%lu bclk=%lu\n", ri.role, (unsigned long)ri.mclk_hz,
           (unsigned long)ri.bclk_hz, ti.role, (unsigned long)ti.mclk_hz, (unsigned long)ti.bclk_hz);
    INIT_OK(probe_b::register_rx(rx));
    INIT_OK(i2s_channel_enable(rx));
    INIT_OK(i2s_channel_enable(tx));
    return true;
}

bool init_codecs() {
    i2c_master_bus_config_t bc{};
    bc.clk_source = I2C_CLK_SRC_DEFAULT; bc.sda_io_num = kSda; bc.scl_io_num = kScl; bc.i2c_port = I2C_NUM_0;
    bc.flags.enable_internal_pullup = true;
    INIT_OK(i2c_new_master_bus(&bc, &bus));
    for (int addr : {0x18, 0x40, 0x5d, 0x14})
        probe::print("I2C probe 0x%02x: %s\n", addr, i2c_master_probe(bus, addr, 20) == ESP_OK ? "ack" : "none");
    INIT_OK(gpio_set_level(kPa, 0));
    INIT_OK(gpio_set_direction(kPa, GPIO_MODE_OUTPUT));
    if(!init_i2s())return false;
    audio_codec_i2c_cfg_t i2c{};
    i2c.port = I2C_NUM_0; i2c.bus_handle = bus; i2c.clock_speed_hz = 400000;
    i2c.addr = ES8311_CODEC_DEFAULT_ADDR;
    auto* dac_ctrl = audio_codec_new_i2c_ctrl(&i2c);
    i2c.addr = ES7210_CODEC_DEFAULT_ADDR;
    auto* adc_ctrl = audio_codec_new_i2c_ctrl(&i2c);
    auto* gpio = audio_codec_new_gpio();
    INIT_OK(dac_ctrl && adc_ctrl && gpio?ESP_OK:ESP_ERR_NO_MEM);
    es8311_codec_cfg_t dc{};
    dc.ctrl_if = dac_ctrl; dc.gpio_if = gpio; dc.codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC;
    dc.pa_pin = -1; dc.use_mclk = true; dc.hw_gain.pa_voltage = 5.0; dc.hw_gain.codec_dac_voltage = 3.3;
    dac = es8311_codec_new(&dc);
    es7210_codec_cfg_t ac{};
    ac.ctrl_if = adc_ctrl;
    ac.mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3 | ES7210_SEL_MIC4;
    adc = es7210_codec_new(&ac);
    if (!dac || !adc) { probe::print("CODEC_INIT_FAILED dac=%p adc=%p\n", dac, adc); return false; }
    esp_codec_dev_sample_info_t f{};
    f.sample_rate = kRate; f.channel = 2; f.bits_per_sample = 16; f.mclk_multiple = 256;
    INIT_OK(dac->set_fs(dac, &f));
    f.bits_per_sample = 32;  // TDM fetched as stereo32 (esp_codec_dev 1.6.2 halves valid bits)
    INIT_OK(adc->set_fs(adc, &f));
    INIT_OK(adc->enable(adc, true));
    INIT_OK(dac->enable(dac, true));
    for (int m : {ES7210_SEL_MIC1, ES7210_SEL_MIC2})
        INIT_OK(adc->set_mic_channel_gain(adc, m, 30.0f));
    INIT_OK(dac->set_vol(dac, -6.0f));
    INIT_OK(dac->mute(dac, false));
    int r0d = 0, r44 = 0, g43 = 0, g44 = 0, g45 = 0, g46 = 0;
    INIT_OK(dac->get_reg(dac, 0x0d, &r0d)); INIT_OK(dac->get_reg(dac, 0x44, &r44));
    INIT_OK(adc->get_reg(adc, 0x43, &g43)); INIT_OK(adc->get_reg(adc, 0x44, &g44)); INIT_OK(adc->get_reg(adc, 0x45, &g45)); INIT_OK(adc->get_reg(adc, 0x46, &g46));
    probe::print("CODEC ES8311 REG0D=0x%02x REG44=0x%02x ES7210 MIC1..4_GAIN=0x%02x 0x%02x 0x%02x 0x%02x\n", r0d, r44, g43, g44, g45, g46);
    INIT_OK(gpio_set_level(kPa, 1));
    return true;
}

struct Capture { std::vector<int16_t> ch[4]; };

// ES7210 TDM wire order MIC1,MIC3,MIC2,MIC4 in stereo32: earlier sample in the high half.
void capture(Capture& c, int ms) {
    const size_t frames = size_t(kRate) * ms / 1000;
    for (auto& v : c.ch) { v.clear(); v.reserve(frames); }
    uint32_t buf[2 * 160];
    size_t got = 0;
    while (got < frames) {
        size_t n = 0;
        ESP_ERROR_CHECK(i2s_channel_read(rx, buf, sizeof(buf), &n, 1000));
        for (size_t i = 0; i + 1 < n / 4 && got < frames; i += 2, ++got) {
            const uint32_t a = buf[i], b = buf[i + 1];
            c.ch[0].push_back(int16_t(a >> 16));
            c.ch[1].push_back(int16_t(b >> 16));
            c.ch[2].push_back(int16_t(a & 0xffff));
            c.ch[3].push_back(int16_t(b & 0xffff));
        }
    }
}

double goertzel_dbfs(const std::vector<int16_t>& x, double hz) {
    const double w = 2 * M_PI * hz / kRate, k = 2 * cos(w);
    double s1 = 0, s2 = 0;
    for (auto v : x) { double s0 = v / 32768.0 + k * s1 - s2; s2 = s1; s1 = s0; }
    const double p = s1 * s1 + s2 * s2 - k * s1 * s2;
    const double amp = 2 * sqrt(std::max(p, 0.0)) / x.size();
    return 20 * log10(amp / sqrt(2) + 1e-12);
}

double rms_dbfs(const std::vector<int16_t>& x) {
    double s = 0, m = 0;
    for (auto v : x) m += v;
    m /= x.size();
    for (auto v : x) { double d = (v - m) / 32768.0; s += d * d; }
    return 10 * log10(s / x.size() + 1e-15);
}

void report(const Capture& c, double hz) {
    for (int i = 0; i < 4; ++i) {
        int peak = 0;
        for (auto v : c.ch[i]) peak = std::max(peak, std::abs(int(v)));
        probe::print("REC ch=MIC%d rms_dbfs=%.1f peak=%d tone_hz=%.0f tone_dbfs=%.1f\n", i + 1, rms_dbfs(c.ch[i]), peak, hz,
               hz > 0 ? goertzel_dbfs(c.ch[i], hz) : -999.0);
    }
}

struct Tone { double hz; int ms; double dbfs; std::atomic<bool> done; };

void play_tone(Tone* t) {
    const double amp = pow(10, t->dbfs / 20) * 32767;
    const size_t total = size_t(kRate) * t->ms / 1000;
    int16_t buf[2 * 160];
    size_t i = 0;
    while (i < total) {
        size_t n = 0;
        for (; n < 160 && i < total; ++n, ++i) {
            double env = std::min({1.0, i / 320.0, (total - i) / 320.0});
            int16_t v = int16_t(amp * env * sin(2 * M_PI * t->hz * i / kRate));
            buf[2 * n] = v; buf[2 * n + 1] = v;
        }
        size_t w = 0;
        ESP_ERROR_CHECK(i2s_channel_write(tx, buf, n * 4, &w, 1000));
    }
    t->done = true;
}

void tone_task(void* p) { play_tone(static_cast<Tone*>(p)); vTaskDelete(nullptr); }

void dump(const Capture& c) {
    // DUMP lines: interleaved MIC1..MIC4 int16 LE, 160 frames per line, base64.
    const size_t frames = c.ch[0].size();
    probe::print("DUMP BEGIN frames=%u channels=4 rate=%d\n", unsigned(frames), kRate);
    static int16_t blk[160 * 4];
    static unsigned char out[2048];
    for (size_t f = 0, line = 0; f < frames; f += 160, ++line) {
        size_t n = std::min<size_t>(160, frames - f);
        for (size_t j = 0; j < n; ++j) for (int ch = 0; ch < 4; ++ch) blk[j * 4 + ch] = c.ch[ch][f + j];
        size_t olen = 0;
        mbedtls_base64_encode(out, sizeof(out), &olen, reinterpret_cast<unsigned char*>(blk), n * 8);
        out[olen] = 0;
        probe::print("DUMP %u %08lx %s\n", unsigned(line), (unsigned long)probe::crc32(out, olen), out);
    }
    probe::print("DUMP END frames=%u\n", unsigned(frames));
}

Capture cap;

std::atomic<bool> loop_busy{false};
unsigned loop_seconds;
void loop_task(void*) {
    Tone t{1000, int(loop_seconds * 1000), -18, false};
    if (xTaskCreate(tone_task, "loop-tone", 4096, &t, 10, nullptr) != pdPASS) {
        loop_busy = false; probe::print("LOOP ERR task\n"); vTaskDelete(nullptr); return;
    }
    uint32_t data[320]; unsigned reads = 0, errors = 0;
    auto end = esp_timer_get_time() + int64_t(loop_seconds) * 1000000;
    while (esp_timer_get_time() < end) {
        size_t n = 0;
        auto rc = i2s_channel_read(rx, data, sizeof data, &n, 1000);
        if (rc != ESP_OK || !n) ++errors;
        ++reads;
    }
    while (!t.done) vTaskDelay(1);
    probe::print("LOOP END reads=%u errors=%u\n", reads, errors);
    loop_busy = false; vTaskDelete(nullptr);
}

void handle(char* line) {
    if (probe_b::command(line, loop_busy)) { probe::print("OK\n"); return; }
    char cmd[16] = {};
    double a = 0, b = 0, c = 0;
    int n = sscanf(line, "%15s %lf %lf %lf", cmd, &a, &b, &c);
    if (n < 1) return;
    if(!audio_ready && (!strcmp(cmd,"loop") || !strcmp(cmd,"tone") || !strcmp(cmd,"rec") ||
        !strcmp(cmd,"duplex") || !strcmp(cmd,"dump") || !strcmp(cmd,"gain") || !strcmp(cmd,"vol"))) {
        probe::print("ERR codec unavailable\nOK\n");return;
    }
    if (loop_busy && (!strcmp(cmd, "tone") || !strcmp(cmd, "rec") || !strcmp(cmd, "duplex") || !strcmp(cmd, "dump"))) {
        probe::print("ERR loop busy\nOK\n"); return;
    }
    if (!strcmp(cmd, "baud")) { probe::baud(unsigned(a)); }
    else if (!strcmp(cmd, "uptime")) { probe::print("UPTIME us=%lld\n", (long long)esp_timer_get_time()); }
    else if (!strcmp(cmd, "flood") || !strcmp(cmd, "ring")) { probe::stream(!strcmp(cmd, "ring"), unsigned(a), unsigned(b)); }
    else if (!strcmp(cmd, "loop")) {
        if (a < 1 || a > 120 || loop_busy) probe::print("ERR loop seconds=1..120 or busy\n");
        else {
            loop_seconds = unsigned(a); loop_busy = true;
            if (xTaskCreate(loop_task, "audio-loop", 4096, nullptr, 10, nullptr) != pdPASS) {
                loop_busy = false; probe::print("ERR loop task\n");
            } else probe::print("LOOP START seconds=%u\n", loop_seconds);
        }
    }
    else
    if (!strcmp(cmd, "tone")) {  // tone <hz> <ms> <dbfs>
        Tone t{a, int(b), c, false};
        play_tone(&t);
        probe::print("TONE done hz=%.0f ms=%d dbfs=%.1f\n", a, int(b), c);
    } else if (!strcmp(cmd, "rec")) {  // rec <ms> <hz>
        capture(cap, int(a));
        report(cap, b);
        probe::print("REC done\n");
    } else if (!strcmp(cmd, "duplex")) {  // duplex <hz> <ms> <dbfs>: play while recording
        static Tone t;
        t.hz = a; t.ms = int(b); t.dbfs = c; t.done = false;
        xTaskCreate(tone_task, "tone", 4096, &t, 5, nullptr);
        capture(cap, int(b));
        while (!t.done) vTaskDelay(1);
        report(cap, a);
        probe::print("DUPLEX done\n");
    } else if (!strcmp(cmd, "dump")) {
        dump(cap);
    } else if (!strcmp(cmd, "gain")) {  // gain <mic 1..4> <db>
        int m = int(a);
        int sel = m == 1 ? ES7210_SEL_MIC1 : m == 2 ? ES7210_SEL_MIC2 : m == 3 ? ES7210_SEL_MIC3 : ES7210_SEL_MIC4;
        probe::print("GAIN mic=%d db=%.0f rc=%d\n", m, b, adc->set_mic_channel_gain(adc, sel, float(b)));
    } else if (!strcmp(cmd, "vol")) {
        probe::print("VOL db=%.1f rc=%d\n", a, dac->set_vol(dac, float(a)));
    } else {
        probe::print("ERR unknown %s\n", cmd);
    }
    probe::print("OK\n");
}
}  // namespace

namespace probe_b {
bool configure_dma(unsigned descriptors,unsigned frames,i2s_chan_handle_t& input,i2s_chan_handle_t& output) {
    audio_ready=false;
    INIT_OK(i2s_channel_disable(::rx));INIT_OK(i2s_channel_disable(::tx));
    INIT_OK(i2s_del_channel(::rx));::rx=nullptr;
    INIT_OK(i2s_del_channel(::tx));::tx=nullptr;
    if(!init_i2s(descriptors,frames))return false;
    input=::rx;output=::tx;audio_ready=true;return true;
}
}
#undef INIT_OK

extern "C" void app_main() {
    probe::console_init();
    probe::print("P4_PROBE boot\n");
    for (int i = 0; i < 8; ++i) probe::print("BOOT %d uptime_us=%lld\n", i, (long long)esp_timer_get_time());
    audio_ready=init_codecs();
    if(!audio_ready)gpio_set_level(kPa,0);
    probe_b::init(bus, rx, tx, adc, dac, audio_ready);
    probe::print("READY boot_lines=8 uptime_us=%lld\nP4_PROBE ready\n", (long long)esp_timer_get_time());
    static char line[1024];
    size_t len = 0;
    while (true) {
        char data[128];
        int count = probe::read(data, sizeof data);
        for (int i = 0; i < count; ++i) {
            char ch = data[i];
            if (ch == '\r') continue;
            if (ch == '\n') { line[len] = 0; if (len) handle(line); len = 0; continue; }
            if (len + 1 < sizeof(line)) line[len++] = ch;
        }
    }
}
