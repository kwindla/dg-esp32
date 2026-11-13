// Offline renderer proof interface. Input is JSON, output SVGs/pose metadata.
// No source bitmap is consumed; similarity registration happens in the HIL tool.
const fs=require('node:fs'),rig=require('./little-voices.cjs');
const requests=JSON.parse(fs.readFileSync(0,'utf8')),result={};
for(const [key,request] of Object.entries(requests)) {
  const p=rig.pose(request.character,request.options||{});
  if(request.mouthExpression)p.features.mouth=rig.expression(p.character,request.mouthExpression).mouth;
  if(request.talkOpen!==undefined)p.features.mouth=rig.lipMap(p.character,rig.expression(p.character,'talking').mouth,request.talkOpen,request.width??.5,request.rounding??.1);
  if(request.sclera!==undefined)p.features.eyes.forEach(e=>e.sclera=request.sclera);
  if(request.eyes)p.features.eyes.forEach(e=>Object.assign(e,request.eyes));
  if(request.gaze)p.gaze=request.gaze;
  const eyeCenter=p.character==='angel'?65:69.5;
  result[key]={svg:rig.render(p,request.part||'all',request.renderOptions||{}),
    eyeCenters:[-1,1].map(side=>p.point([side*eyeCenter+6*p.turn[0],4*p.turn[1]])),
    parameters:JSON.parse(JSON.stringify(p))};
}
process.stdout.write(JSON.stringify(result));
