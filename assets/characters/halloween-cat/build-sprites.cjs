// Export the code-native feature layers as transparent, tightly bounded PNGs.
// Run: node assets/characters/halloween-cat/build-sprites.cjs
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const data = require('./sprites.js');
const out = path.join(__dirname, 'sprites');
fs.mkdirSync(out, {recursive:true});
function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) {
    c ^= b;
    for (let i=0;i<8;i++) c=(c>>>1)^((c&1)?0xedb88320:0);
  }
  return (c^0xffffffff)>>>0;
}
function chunk(type, bytes) {
  const name=Buffer.from(type), size=Buffer.alloc(4), crc=Buffer.alloc(4);
  size.writeUInt32BE(bytes.length); crc.writeUInt32BE(crc32(Buffer.concat([name,bytes])));
  return Buffer.concat([size,name,bytes,crc]);
}
function png(w,h,rgba) {
  const header=Buffer.alloc(13);header.writeUInt32BE(w);header.writeUInt32BE(h,4);
  header[8]=8;header[9]=6;
  const rows=Buffer.alloc(h*(w*4+1));
  for(let y=0;y<h;y++) rgba.copy(rows,y*(w*4+1)+1,y*w*4,(y+1)*w*4);
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),
    chunk('IHDR',header),chunk('IDAT',zlib.deflateSync(rows)),chunk('IEND',Buffer.alloc(0))]);
}
const manifest={...data,layers:{}};
let count=0, bytes=0;
for(const [name,layer] of Object.entries(data.layers)) {
  const [ox,oy,w,h]=layer.bounds;
  manifest.layers[name]={bounds:layer.bounds,frames:{}};
  for(const [frame,rects] of Object.entries(layer.frames)) {
    const rgba=Buffer.alloc(w*h*4);
    for(const [gx,gy,gw,gh,key] of rects) {
      const x=gx*4-ox,y=gy*4-oy,rw=gw*4,rh=gh*4;
      if(x<0||y<0||x+rw>w||y+rh>h) throw Error(`${name}/${frame}: rectangle outside layer bounds`);
      const hex=data.palette[key];
      if(!hex) throw Error(`Unknown palette key: ${key}`);
      const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16));
      for(let py=y;py<y+rh;py++) for(let px=x;px<x+rw;px++) {
        const at=(py*w+px)*4;rgba[at]=rgb[0];rgba[at+1]=rgb[1];rgba[at+2]=rgb[2];rgba[at+3]=255;
      }
    }
    const file=`${name}-${frame}.png`, encoded=png(w,h,rgba);
    fs.writeFileSync(path.join(out,file),encoded);
    manifest.layers[name].frames[frame]=`sprites/${file}`;count++;bytes+=encoded.length;
  }
}
fs.writeFileSync(path.join(__dirname,'animation.json'),JSON.stringify(manifest,null,2)+'\n');
console.log(`Exported ${count} transparent feature sprites (${bytes} PNG bytes) and animation.json.`);
