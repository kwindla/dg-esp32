#include <cmath>
#include <cstdio>
#include <cstdint>
extern "C" void dg_deferred_log(const char*, ...) __attribute__((weak));
#define N 1024
#define SR 16000
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


void mic_report(const int16_t* pcm, float* re, float* im, float* mag) {
        double sr = 0;
        for (int i = 0; i < N; i++) {
            float r = pcm[i] / 32768.0f;
            sr += r * r;
            // The board microphone is on the right slot.
            float w = 0.5f * (1 - cosf(2 * M_PI * i / (N - 1)));
            re[i] = r * w; im[i] = 0;
        }
        float rmsr = 20 * log10f(sqrtf(sr / N) + 1e-9f);
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
        if(dg_deferred_log)dg_deferred_log("MIC rmsR=%.1f peak=%.0f band_snr=%.1f band_power=%.1f",
               rmsr, (float)pk * SR / N, snr, band_db);
}
