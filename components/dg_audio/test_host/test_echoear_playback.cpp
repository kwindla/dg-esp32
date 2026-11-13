#include "codec/audio_codec.h"
#include "driver/gpio.h"
#include <gtest/gtest.h>
#include <atomic>
#include <future>
#include <mutex>
#include <thread>
std::atomic<unsigned> pa_calls{0},pa_value{0},codec_transactions{0};
std::mutex fake_shared_i2c;
namespace dg_audio::codec {
int codec_mute(PlaybackCodecFake*,bool) {
    ++codec_transactions;
    std::lock_guard<std::mutex> bus(fake_shared_i2c);
    return 0;
}
PlaybackCodecFake codec{codec_mute};
PlaybackCodecFake* playback_codec=&codec;
void init() {}
}
unsigned pdMS_TO_TICKS(unsigned n) {return n;}
void vTaskDelay(unsigned n) {std::this_thread::sleep_for(std::chrono::milliseconds(n));}
int gpio_set_level(gpio_num_t pin,unsigned value) {EXPECT_EQ(pin,15);pa_value=value;++pa_calls;return 0;}
TEST(EchoEarPlayback, StartAndIdleCompleteWhileGaugeBusIsStalled) {
    fake_shared_i2c.lock();
    auto playback=std::async(std::launch::async,[]{
        dg_audio::codec::playback_active(true);
        dg_audio::codec::playback_active(false);
    });
    const auto state=playback.wait_for(std::chrono::milliseconds(100));
    fake_shared_i2c.unlock();playback.get();
    EXPECT_EQ(state,std::future_status::ready);
    EXPECT_EQ(codec_transactions,0u);
    EXPECT_EQ(pa_calls,2u);EXPECT_EQ(pa_value,0u);
}
