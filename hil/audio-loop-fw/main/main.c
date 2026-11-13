// Throwaway audio loop test for Waveshare ESP32-S3-Touch-LCD-1.46.
//  - Captures the I2S MEMS mic (WS=2 SCK=15 SD=39) at 16 kHz and every 250 ms
//    prints: "MIC rmsL=<dBFS> rmsR=<dBFS> peak=<Hz> band_snr=<dB>" from a 1024-pt FFT.
//  - Serial commands (newline terminated) over USB-Serial/JTAG:
// Prefix commands with #<seq>; replies are OK/ERR #<seq>.
//      tone <hz> <ms> <amp_percent>   play a sine on the PCM5101 (BCK=48 LRCK=38 DIN=47)
//      exio <bit> <0|1>               drive TCA9554 (0x20 on SDA=11/SCL=10) pin as output
//      quiet <0|1>                    stop/start MIC reports
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "audiotest";
#define SR 16000
#define N 1024

static i2s_chan_handle_t rx_chan, tx_chan;
static i2c_master_bus_handle_t i2c_bus;
static volatile int quiet = 0;

static void mic_init(void) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = 6; cc.dma_frame_num = 256;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, NULL, &rx_chan));
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SR),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = GPIO_NUM_15, .ws = GPIO_NUM_2, .dout = I2S_GPIO_UNUSED, .din = GPIO_NUM_39,
                      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false } },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx_chan, &sc));
    ESP_ERROR_CHECK(i2s_channel_enable(rx_chan));
}

static void spk_init(void) {
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    cc.dma_desc_num = 6; cc.dma_frame_num = 256; cc.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&cc, &tx_chan, NULL));
    i2s_std_config_t sc = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SR),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = { .mclk = I2S_GPIO_UNUSED, .bclk = GPIO_NUM_48, .ws = GPIO_NUM_38, .dout = GPIO_NUM_47, .din = I2S_GPIO_UNUSED,
                      .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false } },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx_chan, &sc));
    ESP_ERROR_CHECK(i2s_channel_enable(tx_chan));
}

static bool play_tone(int hz, int ms, int amp_pct, unsigned seq) {
    const int frames = SR * ms / 1000;
    int16_t *buf = heap_caps_malloc(512 * 2 * sizeof(int16_t), MALLOC_CAP_DEFAULT);
    if (!buf) return false;
    // Acknowledge onset, not completion: the host anchors its recording window here.
    printf("OK #%u\n", seq);
    fflush(stdout);
    double phase = 0, step = 2 * M_PI * hz / SR;
    float amp = 32767.0f * amp_pct / 100.0f;
    for (int done = 0; done < frames;) {
        int n = frames - done > 512 ? 512 : frames - done;
        for (int i = 0; i < n; i++) {
            // 10 ms fade in/out to avoid clicks
            float env = 1.0f;
            int pos = done + i;
            int fade = SR / 100;
            if (pos < fade) env = (float)pos / fade;
            else if (frames - pos < fade) env = (float)(frames - pos) / fade;
            int16_t v = (int16_t)(amp * env * sinf(phase));
            buf[2 * i] = v; buf[2 * i + 1] = v;
            phase += step; if (phase > 2 * M_PI) phase -= 2 * M_PI;
        }
        size_t w = 0;
        esp_err_t err = i2s_channel_write(tx_chan, buf, n * 2 * sizeof(int16_t), &w, 1000);
        if (err != ESP_OK || w != n * 2 * sizeof(int16_t)) {
            ESP_LOGE(TAG, "I2S write: %s, bytes=%u", esp_err_to_name(err), (unsigned)w);
            free(buf);
            return false;
        }
        done += n;
    }
    free(buf);
    printf("TONE done hz=%d ms=%d amp=%d\n", hz, ms, amp_pct);
    return true;
}

static int exio_set(int bit, int level) {
    if (!i2c_bus) {
        i2c_master_bus_config_t cfg = { .i2c_port = -1, .sda_io_num = 11, .scl_io_num = 10, .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7 };
        if (i2c_new_master_bus(&cfg, &i2c_bus) != ESP_OK) return -1;
    }
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = 0x20, .scl_speed_hz = 100000 };
    i2c_master_dev_handle_t dev;
    if (i2c_master_bus_add_device(i2c_bus, &dc, &dev) != ESP_OK) return -1;
    uint8_t reg, val;
    // read output (1) and config (3) regs
    reg = 1; i2c_master_transmit_receive(dev, &reg, 1, &val, 1, 100);
    uint8_t out = level ? (val | (1 << bit)) : (val & ~(1 << bit));
    uint8_t w1[2] = { 1, out }; i2c_master_transmit(dev, w1, 2, 100);
    reg = 3; i2c_master_transmit_receive(dev, &reg, 1, &val, 1, 100);
    uint8_t w3[2] = { 3, (uint8_t)(val & ~(1 << bit)) }; i2c_master_transmit(dev, w3, 2, 100);
    i2c_master_bus_rm_device(dev);
    printf("EXIO%d=%d (out=0x%02X cfg=0x%02X)\n", bit, level, out, (uint8_t)(val & ~(1 << bit)));
    return 0;
}

// Minimal in-place radix-2 FFT on float re/im.
static void fft(float *re, float *im, int n) {
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { float t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
    }
    for (int len = 2; len <= n; len <<= 1) {
        float ang = -2 * M_PI / len, wr = cosf(ang), wi = sinf(ang);
        for (int i = 0; i < n; i += len) {
            float cr = 1, ci = 0;
            for (int j = 0; j < len / 2; j++) {
                int a = i + j, b = i + j + len / 2;
                float xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
                re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
                float ncr = cr * wr - ci * wi; ci = cr * wi + ci * wr; cr = ncr;
            }
        }
    }
}

static void mic_task(void *arg) {
    int32_t *raw = heap_caps_malloc(N * 2 * sizeof(int32_t), MALLOC_CAP_DEFAULT);
    float *re = heap_caps_malloc(N * sizeof(float), MALLOC_CAP_SPIRAM);
    float *im = heap_caps_malloc(N * sizeof(float), MALLOC_CAP_SPIRAM);
    float *mag = heap_caps_malloc((N / 2) * sizeof(float), MALLOC_CAP_SPIRAM);
    assert(raw && re && im && mag);
    while (1) {
        size_t got = 0;
        esp_err_t err = i2s_channel_read(rx_chan, raw, N * 2 * sizeof(int32_t), &got, 1000);
        if (err != ESP_OK || got != N * 2 * sizeof(int32_t)) {
            ESP_LOGE(TAG, "I2S read: %s, bytes=%u", esp_err_to_name(err), (unsigned)got);
            continue;
        }
        if (quiet) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }
        double sl = 0, sr = 0;
        for (int i = 0; i < N; i++) {
            float l = raw[2 * i] / 2147483648.0f, r = raw[2 * i + 1] / 2147483648.0f;
            sl += l * l; sr += r * r;
            // The board microphone is on the right slot.
            float w = 0.5f * (1 - cosf(2 * M_PI * i / (N - 1)));
            re[i] = r * w; im[i] = 0;
        }
        float rmsl = 20 * log10f(sqrtf(sl / N) + 1e-9f), rmsr = 20 * log10f(sqrtf(sr / N) + 1e-9f);
        fft(re, im, N);
        int pk = 20;
        float pkv = 0, signal = 0, noise = 0;
        for (int k = 1; k < N / 2; k++) {
            float hz = (float)k * SR / N;
            mag[k] = re[k] * re[k] + im[k] * im[k];
            if (hz >= 300 && hz <= 4000) {
                if (mag[k] > pkv) { pkv = mag[k]; pk = k; }
                // Host-to-device acceptance stimulus is exactly 1000 Hz.
                if (fabsf(hz - 1000) <= 30) signal += mag[k];
                else {
                    bool harmonic = false;
                    for (int h = 2; h * 1000 <= 4000; h++)
                        if (fabsf(hz - h * 1000) <= 30) harmonic = true;
                    if (!harmonic) noise += mag[k];
                }
            }
        }
        float snr = 10 * log10f(fmaxf(signal, 1e-24f) / fmaxf(noise, 1e-24f));
        // Hann sum of squares = 3*(N-1)/8; normalized one-sided band power.
        float power = 2 * signal / (N * (3.0f * (N - 1) / 8));
        float band_db = 10 * log10f(fmaxf(power, 1e-24f));
        printf("MIC rmsL=%.1f rmsR=%.1f peak=%.0f band_snr=%.1f band_power=%.1f\n",
               rmsl, rmsr, (float)pk * SR / N, snr, band_db);
        vTaskDelay(pdMS_TO_TICKS(150));
    }
}

void app_main(void) {
    usb_serial_jtag_driver_config_t ucfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&ucfg));
    usb_serial_jtag_vfs_use_driver();
    setvbuf(stdin, NULL, _IONBF, 0);
    mic_init();
    spk_init();
    xTaskCreatePinnedToCore(mic_task, "mic", 8192, NULL, 5, NULL, 1);
    printf("READY audiotest\n");
    char line[128];
    while (fgets(line, sizeof(line), stdin)) {
        unsigned seq = 0;
        int used = 0, a = 0, b = 0, c = 50;
        bool ok = false;
        if (sscanf(line, "#%u %n", &seq, &used) != 1 || !used) {
            printf("ERR #0 missing sequence\n");
            continue;
        }
        const char *cmd = line + used;
        if (sscanf(cmd, "tone %d %d %d", &a, &b, &c) >= 2) {
            if (a >= 100 && a <= 4000 && b > 0 && b <= 10000 && c > 0 && c <= 100) {
                ok = play_tone(a, b, c, seq);
                printf("%s #%u\n", ok ? "DONE" : "ERR", seq);
                continue;
            }
        } else if (sscanf(cmd, "exio %d %d", &a, &b) == 2) {
            if (a >= 0 && a <= 7 && (b == 0 || b == 1)) ok = exio_set(a, b) == 0;
        } else if (sscanf(cmd, "quiet %d", &a) == 1 && (a == 0 || a == 1)) {
            quiet = a;
            ok = true;
        } else if (strcmp(cmd, "ping\n") == 0) {
            ok = true;
        }
        printf("%s #%u\n", ok ? "OK" : "ERR", seq);
    }
}
