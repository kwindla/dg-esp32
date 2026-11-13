#pragma once
#include <string>
#include "dg_wifi_observe.h"
std::string performance_snapshot(std::string transport);
void performance_cert_duration(unsigned us);

void performance_init();
void performance_compare_sampling(bool enabled);

std::string power_status_json();
std::string wifi_observation_status_json(const dg_wifi_observation_status_t&);
std::string wifi_observation_row_json(const dg_wifi_observation_t&);
#if CONFIG_DG_HEAP_ATTRIBUTION
#include "heap_attribution.h"
#endif
