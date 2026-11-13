#pragma once
namespace dg_audio {
// Snapshot of initialized engine and current runtime signal-conditioning values.
struct Capability {bool aec=false;unsigned ref_gain_db=0,guard_ms=0;};
}
