#pragma once
#include <atomic>
#include <mutex>
// Shared by the held gauge transaction and fake codec I2C, including the old
// playback_active body used as a negative control. New code must make 0 calls.
extern std::mutex fake_shared_i2c;
extern std::atomic<unsigned> codec_transactions;
namespace dg_audio::codec {
struct PlaybackCodecFake {int (*mute)(PlaybackCodecFake*,bool);};
extern PlaybackCodecFake* playback_codec;
void init();
}
unsigned pdMS_TO_TICKS(unsigned);
void vTaskDelay(unsigned);
