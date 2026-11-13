#pragma once
#include "esp_err.h"
#include <cstdint>
#include "dg_duo/protocol.h"
namespace dg {
struct DuoSettings {uint32_t version=1;char origin[192]{};};
void duo_settings_init();
void duo_settings_poll(); // existing demo-config owner, internal stack
dg_duo::Persona duo_last_character();
void duo_character_ack(dg_duo::Persona);
DuoSettings duo_settings();
esp_err_t duo_settings_save(const DuoSettings&);
bool duo_valid_origin(const char*)noexcept;
}
