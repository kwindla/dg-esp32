#include "check.h"
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include <csignal>
int main(){
    int calls=0;CHECK(++calls==1);if(calls!=1)return 1;
    auto child=fork();if(child<0)return 1;
    if(!child){rlimit limit{0,0};setrlimit(RLIMIT_CORE,&limit);CHECK(false);_exit(0);}
    int status=0;if(waitpid(child,&status,0)!=child)return 1;
    return WIFSIGNALED(status) && WTERMSIG(status)==SIGABRT?0:1;
}
