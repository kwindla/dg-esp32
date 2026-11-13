#include "dg_display/flash_store.h"
#include <cstring>
#include <cstdio>
#include <cerrno>
#include <algorithm>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
namespace dg_display {
namespace {
Error io_error(){return errno==ENOSPC?Error::FlashFull:Error::StorageUnavailable;}
Error validate(const char* hash,const uint8_t* data,size_t bytes){char actual[65];if(!digest(data,bytes,actual) || std::strcmp(hash,actual))return Error::Hash;Image image;if(!image.open(data,bytes))return Error::InvalidDgi;return Error::None;}
}
bool FlashStore::path(const char* hash,bool temporary,char* out,size_t cap)const noexcept {if(!valid_hash(hash) || !root_[0])return false;int n=std::snprintf(out,cap,"%s/%s%s",root_,hash,temporary?".tmp":"");return n>0 && size_t(n)<cap;}
FlashItem* FlashStore::find(const char* hash) noexcept {for(auto& i:items)if(i.hash[0] && !std::strcmp(hash,i.hash))return &i;return nullptr;}
size_t FlashStore::max_admissible()const noexcept {if(!mounted_ || capacity_<used+temp_bytes+512)return 0;size_t available=capacity_-used-temp_bytes-512;if(free_bytes){size_t physical=free_bytes();available=std::min(available,physical>16384?physical-16384:0);}return std::min(size_t(524288),available);}
Error FlashStore::mount(const char* root,size_t cap,uint8_t* scratch,size_t scratch_cap,bool cleanup) noexcept {
    if(!root || std::strlen(root)>=sizeof(root_) || !scratch)return Error::Malformed;
    std::strcpy(root_,root);capacity_=cap;mounted_=false;count=0;used=0;temp_bytes=0;for(auto& i:items)i=FlashItem{};
    DIR* directory=opendir(root_);if(!directory)return Error::StorageUnavailable;
    Error failure=Error::None;
    while(auto* node=readdir(directory)){
        size_t len=std::strlen(node->d_name);bool temporary=len==68 && !std::strcmp(node->d_name+64,".tmp");if(len!=64 && !temporary)continue;
        char hash[65];std::memcpy(hash,node->d_name,64);hash[64]=0;if(!valid_hash(hash))continue;
        char filename[240];if(!path(hash,temporary,filename,sizeof(filename))){failure=Error::Malformed;break;}
        struct stat info{};if(stat(filename,&info) || !S_ISREG(info.st_mode)){failure=Error::StorageUnavailable;break;}
        if(temporary){if(cleanup){if((write_allowed && !write_allowed()) || unlink(filename)){failure=Error::StorageUnavailable;break;}}else temp_bytes+=size_t(info.st_size);continue;}
        if(count==128){failure=Error::CatalogFull;break;}
        auto& item=items[count++];std::memcpy(item.hash,hash,65);item.bytes=uint32_t(std::min(uint64_t(UINT32_MAX),uint64_t(info.st_size)));used+=size_t(info.st_size);
        if(info.st_size<=0 || uint64_t(info.st_size)>524288 || uint64_t(info.st_size)>scratch_cap){item.error=Error::InvalidDgi;continue;}
        int fd=open(filename,O_RDONLY);if(fd<0){item.error=Error::StorageUnavailable;continue;}size_t at=0;bool good=true;
        while(at<size_t(info.st_size)){ssize_t n=::read(fd,scratch+at,size_t(info.st_size)-at);if(n<0 && errno==EINTR)continue;if(n<=0){good=false;break;}at+=size_t(n);}uint8_t extra;ssize_t tail=good?::read(fd,&extra,1):-1;int closing=close(fd);
        item.error=good && tail==0 && closing==0?validate(hash,scratch,at):Error::StorageUnavailable;item.valid=item.error==Error::None;
    }
    if(closedir(directory) && failure==Error::None)failure=Error::StorageUnavailable;
    mounted_=failure==Error::None;++revision;return failure;
}
Error FlashStore::install(const char* hash,const uint8_t* data,size_t bytes) noexcept {
    if(!valid_hash(hash) || !data || !bytes || bytes>524288)return Error::Malformed;
    if(!mounted_ || (write_allowed && !write_allowed()))return Error::StorageUnavailable;
    Error checked=validate(hash,data,bytes);if(checked!=Error::None)return checked;
    if(auto* old=find(hash)){if(old->valid)return Error::None;return old->error;}
    FlashItem* target=nullptr;for(auto& i:items)if(!i.hash[0]){target=&i;break;}if(!target)return Error::CatalogFull;
    if(bytes>max_admissible())return Error::FlashFull;
    char final[240],temp[240];if(!path(hash,false,final,sizeof(final)) || !path(hash,true,temp,sizeof(temp)))return Error::Malformed;
    int fd=open(temp,O_WRONLY|O_CREAT|O_TRUNC,0600);if(fd<0)return io_error();temp_bytes=bytes;Error error=Error::None;size_t at=0;
    while(at<bytes){if((write_allowed && !write_allowed()) || fault==Fault::Write){errno=fault==Fault::Write?ENOSPC:EBUSY;error=io_error();break;}ssize_t n=write(fd,data+at,std::min(size_t(4096),bytes-at));if(n<0 && errno==EINTR)continue;if(n<=0){error=io_error();break;}at+=size_t(n);if(checkpoint)checkpoint(1,at);}
    if(error==Error::None && (fault==Fault::Sync || fsync(fd)))error=Error::StorageUnavailable;
    int closed=close(fd);if(error==Error::None && (closed || fault==Fault::Close))error=Error::StorageUnavailable;
    if(error==Error::None && checkpoint)checkpoint(2,bytes);
    if(error==Error::None && ((write_allowed && !write_allowed()) || fault==Fault::Rename || rename(temp,final)))error=Error::StorageUnavailable;
    if(error!=Error::None){bool removed=(!write_allowed || write_allowed()) && unlink(temp)==0;temp_bytes=removed?0:at;return error;}
    if(checkpoint)checkpoint(3,bytes);
    *target=FlashItem{};std::memcpy(target->hash,hash,65);target->bytes=uint32_t(bytes);target->valid=true;used+=bytes;temp_bytes=0;++count;++revision;++writes;return Error::None;
}
Error FlashStore::read(const char* hash,uint8_t* data,size_t cap,size_t& size) noexcept {
    size=0;if(!valid_hash(hash))return Error::Malformed;if(!mounted_)return Error::StorageUnavailable;auto* item=find(hash);if(!item)return Error::NotLoaded;if(!item->valid)return item->error;if(cap<item->bytes)return Error::RamBudget;
    char filename[240];if(!path(hash,false,filename,sizeof(filename)))return Error::Malformed;int fd=open(filename,O_RDONLY);if(fd<0)return io_error();Error error=Error::None;
    while(size<item->bytes){ssize_t n=::read(fd,data+size,std::min(size_t(4096),size_t(item->bytes)-size));if(n<0 && errno==EINTR)continue;if(n<=0){error=Error::StorageUnavailable;break;}size+=size_t(n);}uint8_t extra;ssize_t tail=error==Error::None?::read(fd,&extra,1):-1;int closing=close(fd);if(error==Error::None && (tail!=0 || closing))error=Error::InvalidDgi;if(error==Error::None)error=validate(hash,data,size);return error;
}
Error FlashStore::erase(const char* hash) noexcept {
    if(!valid_hash(hash))return Error::Malformed;
    if(!mounted_ || (write_allowed && !write_allowed()))return Error::StorageUnavailable;
    char filename[240];if(!path(hash,true,filename,sizeof(filename)))return Error::Malformed;
    if(unlink(filename) && errno!=ENOENT)return io_error();
    temp_bytes=0;
    auto* item=find(hash);if(!item)return Error::None;
    if(!path(hash,false,filename,sizeof(filename)))return Error::Malformed;
    if(unlink(filename))return io_error();
    used-=item->bytes;*item=FlashItem{};--count;++revision;return Error::None;
}
}
