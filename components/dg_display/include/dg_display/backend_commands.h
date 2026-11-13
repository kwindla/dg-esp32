#pragma once
namespace dg_display {
// Headless transport must advertise display-ready without a panel/demo.
// Physical backends retain their existing explicit initialization lifecycle.
void backend_boot();
void command(const char* arguments,bool idle,void (*output)(const char*));
// Backend-specific diagnostics are rejected before shared runtime state checks.
bool backend_command_supported(const char* action) noexcept;
}
