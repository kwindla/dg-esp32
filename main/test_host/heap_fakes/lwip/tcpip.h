#pragma once
using err_t=int;
constexpr int ERR_OK=0,ERR_VAL=-1;
struct tcpip_api_call_data {};
err_t tcpip_api_call(err_t (*)(tcpip_api_call_data*),tcpip_api_call_data*);
