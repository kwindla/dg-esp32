#pragma once
#include "driver/spi_master.h"
namespace dg_display {
bool qspi_initialized() noexcept;
spi_device_handle_t qspi_device() noexcept;
}
