#pragma once
struct PthreadConfig {struct Config{int pin_to_core,prio,stack_size,stack_alloc_caps;const char* thread_name;};Config current(){return {};}void set(Config){}};
