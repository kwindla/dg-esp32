#pragma once
constexpr unsigned TCP_MSL=60000,MEMP_NUM_TCP_PCB=16;
struct tcp_pcb {tcp_pcb* next;};
extern tcp_pcb *tcp_active_pcbs,*tcp_tw_pcbs,*tcp_bound_pcbs;
