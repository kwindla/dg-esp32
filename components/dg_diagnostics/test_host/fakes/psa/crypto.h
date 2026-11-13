#pragma once
#include <openssl/evp.h>
#include <cstddef>
#include "esp_heap_caps.h"
using psa_status_t=int;
struct psa_hash_operation_t {EVP_MD_CTX* ctx;};
#define PSA_HASH_OPERATION_INIT {nullptr}
#define PSA_SUCCESS 0
#define PSA_ALG_SHA_256 1
inline void checked_operation(psa_hash_operation_t* p) {
    hash_fake::operations.push_back({p,sizeof(*p)});
    if(!hash_fake::is_internal(p,sizeof(*p)))throw std::runtime_error("SHA operation outside internal allocation");
}
inline int psa_hash_setup(psa_hash_operation_t* p,int) {
    checked_operation(p);
    p->ctx=EVP_MD_CTX_new();return p->ctx && EVP_DigestInit_ex(p->ctx,EVP_sha256(),nullptr)==1?0:-1;
}
inline int psa_hash_update(psa_hash_operation_t* p,const unsigned char* data,size_t size) {
    checked_operation(p);
    hash_fake::updates.push_back({data,size});
    if(!hash_fake::is_internal(data,size))throw std::runtime_error("SHA input outside internal scratch");
    for(const auto& caller:hash_fake::callers)
        if(hash_fake::inside(caller,data,size))throw std::runtime_error("SHA input aliases caller");
    return EVP_DigestUpdate(p->ctx,data,size)==1?0:-1;
}
inline int psa_hash_finish(psa_hash_operation_t* p,unsigned char* data,size_t size,size_t* written) {
    checked_operation(p);
    if(!hash_fake::is_internal(data,32))throw std::runtime_error("SHA digest outside internal allocation");
    unsigned n=0;if(size<32 || EVP_DigestFinal_ex(p->ctx,data,&n)!=1)return -1;*written=n;return 0;
}
inline int psa_hash_abort(psa_hash_operation_t* p) {
    checked_operation(p);EVP_MD_CTX_free(p->ctx);p->ctx=nullptr;return 0;
}
