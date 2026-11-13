#pragma once
#include "pipecat/esp_peer/stat_json.h"
void cache_metrics_enable(bool enabled);
pipecat::esp_peer::StatJson cache_metrics_snapshot();
