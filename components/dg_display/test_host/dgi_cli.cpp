#include "dg_display/dgi.h"
#include <chrono>
#include <cstdio>
#include <fstream>
#include <vector>
#include <cstring>
using namespace dg_display;
int main(int argc,char** argv){
 if(argc<2)return 2;
 std::ifstream in(argv[1],std::ios::binary);std::vector<uint8_t> bytes((std::istreambuf_iterator<char>(in)),{});Image image;
 if(!image.open(bytes.data(),bytes.size()))return 3;
 struct Guarded{uint32_t left=0x12345678;Pixel row[412];uint32_t right=0x87654321;} result;
 if(argc>2 && !std::strcmp(argv[2],"bench")){
  auto run=[&](unsigned x,unsigned y,unsigned w,unsigned h){auto start=std::chrono::steady_clock::now();for(unsigned n=0;n<100;++n)for(unsigned row=y;row<y+h;++row)if(!image.crop(row,x,w,result.row,412))return int64_t(-1);return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-start).count()/100;};
  printf("{\"full_us\":%lld}\n",(long long)run(0,0,image.width(),image.height()));
  if(image.width()==412 && image.height()==412)printf("{\"dirty_us\":%lld}\n",(long long)run(92,176,224,56));
 }else{
  unsigned x=0,y=0,w=image.width(),h=image.height();if(argc==6){x=std::stoul(argv[2]);y=std::stoul(argv[3]);w=std::stoul(argv[4]);h=std::stoul(argv[5]);}
  if(y>image.height() || h>image.height()-y)return 4;
  for(unsigned row=y;row<y+h;++row){if(!image.crop(row,x,w,result.row,412))return 4;for(unsigned i=0;i<w;++i){uint8_t p[]{uint8_t(result.row[i].color>>8),uint8_t(result.row[i].color),result.row[i].alpha};if(fwrite(p,1,3,stdout)!=3)return 5;}}
 }
 return result.left==0x12345678 && result.right==0x87654321?0:6;
}
