// Evaluate the ported Little Voices paths/gradients, then use librsvg/Cairo.
const path=require('node:path'),{spawnSync}=require('node:child_process');
function build(dir,data) {
  const result=spawnSync('python3',[path.join(__dirname,'duo-raster.py'),dir],
    {input:JSON.stringify(data),encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
  if(result.status!==0)throw Error(result.stderr||result.error||'Rasterizer failed');
  process.stdout.write(result.stdout);
}
module.exports={build};
