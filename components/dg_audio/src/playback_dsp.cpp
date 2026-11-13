#include "dg_audio/playback_dsp.h"
#include <cmath>
#include <algorithm>
#include <stdexcept>
namespace dg_audio {
PlaybackDsp::PlaybackDsp(float gain,float ceiling,float hz,float rate):gain_(gain),ceiling_(ceiling),knee_(ceiling*.8f) {
    if(!std::isfinite(gain)||gain<0||!std::isfinite(ceiling)||ceiling<=0||ceiling>1||!std::isfinite(hz)||hz<0||!std::isfinite(rate)||rate<=0||hz>=rate/2)
        throw std::invalid_argument("Invalid playback DSP configuration");
    if(hz>0) {
        float w=2*3.14159265358979323846f*hz/rate,c=std::cos(w),alpha=std::sin(w)/std::sqrt(2.0f),norm=1/(1+alpha);
        b0_=(1+c)*.5f*norm;b1_=-(1+c)*norm;b2_=b0_;a1_=-2*c*norm;a2_=(1-alpha)*norm;
    }
}
PlaybackDsp::PlaybackDsp(float ceiling,float hz,const SpeechLevelConfig& c,float rate)
    : PlaybackDsp(1,ceiling,hz,rate) {
    for(float v : {c.target_dbfs,c.seed_dbfs,c.max_gain_db,c.activity_dbfs,c.level_ms,
                   c.attack_ms,c.release_ms,c.lookahead_ms,c.limiter_release_ms,
                   c.compression_ratio,c.compression_threshold_dbfs})
        if(!std::isfinite(v)) throw std::invalid_argument("Nonfinite speech DSP setting");
    if(c.seed_dbfs>0 || c.seed_dbfs<-90 || c.target_dbfs>0 || c.target_dbfs<-60 || c.max_gain_db<0 || c.max_gain_db>36 ||
       c.activity_dbfs>c.target_dbfs || c.activity_dbfs<-90 || c.level_ms<=0 ||
       c.attack_ms<=0 || c.release_ms<=0 || c.lookahead_ms<=0 || c.lookahead_ms>2.5f ||
       c.limiter_release_ms<=0 || rate>32000 || c.compression_ratio<1 || c.compression_ratio>4 ||
       c.compression_threshold_dbfs>0 || c.compression_threshold_dbfs<-30)
        throw std::invalid_argument("Invalid speech DSP setting");
    speech_=true;
    compression_threshold_=std::pow(10.f,c.compression_threshold_dbfs/20);
    compression_width_=compression_threshold_*.25f;compression_slope_=1/c.compression_ratio;
    compression_knee_reciprocal_=1/(2*compression_width_);
    target_=std::pow(10.0f,c.target_dbfs/20);
    max_gain_=std::pow(10.0f,c.max_gain_db/20);
    // A -28.9 dBFS effective-source seed includes about 3 dB of compression
    // make-up for typical -26 dBFS post-HPF TTS. Hold the normal Kconfig
    // dynamics from startup; fast onset adaptation modulates short speech.
    level_power_=std::pow(10.f,c.seed_dbfs/10);
    desired_gain_=agc_gain_=std::clamp(target_/std::sqrt(level_power_),.1f,max_gain_);
    activity_power_=std::pow(10.0f,c.activity_dbfs/10);
    window_size_=std::max(1u,unsigned(std::lround(rate*.02f)));
    level_alpha_=1-std::exp(-20/c.level_ms);
    attack_alpha_=1-std::exp(-1000/(rate*c.attack_ms));
    release_alpha_=1-std::exp(-1000/(rate*c.release_ms));
    limiter_alpha_=1-std::exp(-1000/(rate*c.limiter_release_ms));
    ramp_samples_=std::max(size_t(1),size_t(std::lround(rate*c.lookahead_ms/1000)));
    ramp_reciprocal_=1.f/ramp_samples_;
    delay_samples_=2*ramp_samples_;
    ramp_.fill(1);ramp_sum_=float(ramp_samples_);
}
void PlaybackDsp::reset() {
    ++resets_;
    z1_=z2_=0; window_power_=delivered_power_=0; window_samples_=0; active_=false;
    delay_.fill(0); activity_.fill(false); delay_at_=peak_head_=peak_count_=0;
    sample_index_=0;limit_gain_=1;pending_samples_=0;ramp_at_=0;hold_remaining_=0;
    ramp_.fill(1);ramp_sum_=float(ramp_samples_);
}
size_t PlaybackDsp::flush(int16_t* pcm,size_t available) {
    size_t count=std::min(pending_samples_,available);
    for(size_t i=0;i<count;++i) pcm[i]=static_cast<int16_t>(std::lround(sample(0)*32767));
    pending_samples_-=count;
    return count;
}
float PlaybackDsp::sample(float x) {
    float y=b0_*x+z1_;z1_=b1_*x-a1_*y+z2_;z2_=b2_*x-a2_*y;
    if(speech_) {
        window_power_+=y*y;
        if(++window_samples_==window_size_) {
            float power=window_power_/window_size_;
            active_=power>=activity_power_;
            if(active_) {
                // Estimate compressed power BEFORE limiting, removing the
                // current normalizer gain. Do not chase power removed by
                // the safety limiter when the requested target is unreachable.
                float effective=delivered_power_/window_size_/(agc_gain_*agc_gain_);
                level_power_+=level_alpha_*(effective-level_power_);
                desired_gain_=std::clamp(target_/std::sqrt(level_power_),.1f,max_gain_);
            }
            window_samples_=0;window_power_=delivered_power_=0;
        }
        if(active_) agc_gain_+=(desired_gain_<agc_gain_?attack_alpha_:release_alpha_)*(desired_gain_-agc_gain_);
        y*=agc_gain_;
        // Optional light, C1 soft-knee compression of normalized speech peaks.
        float magnitude=std::abs(y),start=compression_threshold_-compression_width_*.5f;
        if(compression_slope_<1 && magnitude>start) {
            float excess=magnitude-start;
            float compressed=magnitude>=start+compression_width_?
                compression_threshold_+(magnitude-compression_threshold_)*compression_slope_:
                magnitude-(1-compression_slope_)*excess*excess*compression_knee_reciprocal_;
            y=std::copysign(compressed,y);
        }
        delivered_power_+=y*y;
        magnitude=std::abs(y);
        // Monotonic deque: amortized O(1) maximum over delayed sample and
        // all future samples available in the <=5 ms look-ahead window.
        while(peak_count_ && uint32_t(sample_index_-peak_indices_[peak_head_])>delay_samples_) {
            peak_head_=(peak_head_+1)%capacity;--peak_count_;
        }
        while(peak_count_ && peaks_[(peak_head_+peak_count_-1)%capacity]<=magnitude) --peak_count_;
        size_t tail=(peak_head_+peak_count_)%capacity;
        peaks_[tail]=magnitude;peak_indices_[tail]=sample_index_;++peak_count_;++sample_index_;
        const float required=peaks_[peak_head_]>ceiling_?ceiling_/peaks_[peak_head_]:1;
        if(required<=limit_gain_) {limit_gain_=required;hold_remaining_=ramp_samples_;}
        else if(hold_remaining_) --hold_remaining_;
        else limit_gain_=std::min(required,limit_gain_+limiter_alpha_*(1-limit_gain_));
        // Min-filter over 2L, then an L-sample boxcar. A peak ramps gain down
        // over L samples and holds it for at least L before the 75 ms release.
        // For output index n-2L, EVERY boxcar member's min-filter contains that
        // sample, so their average also guarantees its ceiling.
        ramp_sum_+=limit_gain_-ramp_[ramp_at_];ramp_[ramp_at_]=limit_gain_;
        if(++ramp_at_==ramp_samples_) ramp_at_=0;
        float delayed=delay_[delay_at_];bool delayed_active=activity_[delay_at_];
        delay_[delay_at_]=y;activity_[delay_at_]=active_;
        if(++delay_at_==delay_samples_) delay_at_=0;
        float smoothed=std::clamp(ramp_sum_*ramp_reciprocal_,0.f,1.f);
        if(delayed_active) {
            ++active_samples_;
            if(smoothed<.999f) ++engagements_;
            if(std::abs(delayed)>ceiling_) ++peak_exceedances_;
        }
        // Protect the mathematical bound from floating-point boxcar drift.
        if(std::abs(delayed)>ceiling_) smoothed=std::min(smoothed,ceiling_/std::abs(delayed));
        float output=delayed*smoothed;
        return output;
    }
    y*=gain_;float magnitude=std::abs(y);
    if(magnitude>knee_) {
        ++engagements_;
        float width=ceiling_-knee_,excess=magnitude-knee_;
        // Rational saturation: value and slope match the linear region.
        // No transcendental per-sample work on the codec's core.
        y=std::copysign(knee_+width*excess/(width+excess),y);
    }
    return y;
}
void PlaybackDsp::process(int16_t* pcm,size_t n) {
    for(size_t i=0;i<n;++i) pcm[i]=static_cast<int16_t>(std::lround(sample(pcm[i]/32768.0f)*32767));
    if(n) pending_samples_=delay_samples_;
}
}
