/* Daily Little Voices 7f5613c renderer port, BSD-2 (pack NOTICE).
 * Source: FaceGeometry/FaceParameters/FaceExpressions/FaceRig/StageRenderer/
 * StageColors/StagePalette. Evaluate settled poses; the runtime energy driver
 * replaces live springs and phonemes. No reference bitmap is sampled here.
 */
const clamp=(x,a=0,b=1)=>Math.min(Math.max(x,a),b),mix=(a,b,t)=>a+(b-a)*t;
const smooth=(a,b,x)=>{const t=clamp((x-a)/(b-a));return t*t*(3-2*t);};
const add=(a,b)=>[a[0]+b[0],a[1]+b[1]],sub=(a,b)=>[a[0]-b[0],a[1]-b[1]],mul=(a,s)=>[a[0]*s,a[1]*s];
const lerp=(a,b,t)=>add(a,mul(sub(b,a),t)),length=a=>Math.hypot(...a),unit=a=>mul(a,1/Math.max(length(a),1e-12));
const rotate=(p,a)=>[p[0]*Math.cos(a)-p[1]*Math.sin(a),p[0]*Math.sin(a)+p[1]*Math.cos(a)];
const deg=Math.PI/180,fmt=n=>Number(n.toFixed(7));
const quad=(a,b,c,t)=>add(add(mul(a,(1-t)**2),mul(b,2*(1-t)*t)),mul(c,t*t));
const cubic=(a,b,c,d,t)=>add(add(mul(a,(1-t)**3),mul(b,3*(1-t)**2*t)),add(mul(c,3*(1-t)*t*t),mul(d,t**3)));
class Path {
  constructor(){this.commands=[];}
  M(p){this.commands.push(['M',p]);return this;} L(p){this.commands.push(['L',p]);return this;}
  Q(c,p){this.commands.push(['Q',c,p]);return this;} C(a,b,p){this.commands.push(['C',a,b,p]);return this;}
  Z(){this.commands.push(['Z']);return this;}
  append(p){this.commands.push(...p.commands);return this;}
  map(f){const p=new Path();p.commands=this.commands.map(([c,...pts])=>[c,...pts.map(f)]);return p;}
  svg(){return this.commands.map(([c,...pts])=>c+pts.map(p=>p.map(fmt).join(' ')).join(' ')).join(' ');}
}
function ellipse(c,rx,ry) {
  const p=new Path(),k=.5522847498,v=(x,y)=>add(c,[x,y]);
  return p.M(v(rx,0)).C(v(rx,k*ry),v(k*rx,ry),v(0,ry)).C(v(-k*rx,ry),v(-rx,k*ry),v(-rx,0))
    .C(v(-rx,-k*ry),v(-k*rx,-ry),v(0,-ry)).C(v(k*rx,-ry),v(rx,-k*ry),v(rx,0)).Z();
}
function poly(points,closed=true){const p=new Path().M(points[0]);points.slice(1).forEach(v=>p.L(v));return closed?p.Z():p;}
function smoothPath(points){const p=new Path().M(points[0]);for(let i=0;i<points.length-1;i++) {
  const a=points[Math.max(i-1,0)],b=points[i],c=points[i+1],d=points[Math.min(i+2,points.length-1)];
  p.C(add(b,mul(sub(c,a),1/6)),sub(c,mul(sub(d,b),1/6)),c);
}return p;}
const stopXML=stops=>stops.map(([at,color,a=1])=>`<stop offset="${at}" stop-color="${color}" stop-opacity="${a}"/>`).join('');
class SVG {
  constructor(){this.defs=[];this.n=0;}
  def(body){const id='v'+this.n++;this.defs.push(body(id));return `url(#${id})`;}
  clip(p){return this.def(id=>`<clipPath id="${id}"><path d="${p.svg()}"/></clipPath>`);}
  radial(stops,c,rx,ry){return this.def(id=>`<radialGradient id="${id}" gradientUnits="userSpaceOnUse" cx="0" cy="0" r="1" gradientTransform="translate(${c}) scale(${rx},${ry})" color-interpolation="sRGB">${stopXML(stops)}</radialGradient>`);}
  linear(stops,a,b){return this.def(id=>`<linearGradient id="${id}" gradientUnits="userSpaceOnUse" x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" color-interpolation="sRGB">${stopXML(stops)}</linearGradient>`);}
  fill(p,color,alpha=1,extra=''){return `<path d="${p.svg()}" fill="${color}" fill-opacity="${alpha}" ${extra}/>`;}
  stroke(p,color,width,alpha=1,cap='round'){return `<path d="${p.svg()}" fill="none" stroke="${color}" stroke-width="${width}" stroke-opacity="${alpha}" stroke-linecap="${cap}" stroke-linejoin="round"/>`;}
  wrap(body){return `<svg xmlns="http://www.w3.org/2000/svg" width="412" height="412" viewBox="0 0 412 412"><defs>${this.defs.join('')}</defs>${body}</svg>`;}
}
const field={angel:[[0,'#FEEFA2'],[.2,'#FDEA96'],[.32,'#FDE385'],[.5,'#FDDD78'],[.62,'#FCD666'],[.8,'#FCCF58'],[1,'#F8C24A']],
  devil:[[0,'#FC8C1E'],[.15,'#F97A1B'],[.31,'#F45815'],[.48,'#EE4A16'],[.66,'#D83A17'],[.85,'#BE3018'],[1,'#9A221A']]};
const colors={angel:{ink:'#2A1206',sclera:'#FFFFFF',highlight:'#FFFFFF',blush:'#FDCB63'},
  devil:{ink:'#200A03',sclera:'#FFF6E2',highlight:'#FFF6E8',blush:'#FFA27A'}};
const eyeDefaults=()=>({open:1,size:1,sclera:0,iris:.6,inward:0,highlight:1,highlight2:0,sparkle:0,starry:0,lidTop:0,lidAngle:0,lidInk:0,lowerLid:0,closed:0,arcUp:1,arcDown:0,chevron:0});
const browDefaults=()=>({dy:0,angle:0,arch:1,alpha:1});
const mouthDefaults=(m={})=>({width:54,open:0,corner:0,skew:0,round:0,tongue:0,fang:0,fangX:0,shiftX:0,line:5,...m});
// FaceExpressions.swift neutral, resting, talking, squished, surprised, happy.
function expression(c,kind='neutral') {
  const angel=c==='angel',eye=eyeDefaults(),brow=browDefaults();
  let mouth,accessories={blush:1,blushSize:1,dashes:angel?1:0,motion:angel?0:1,haloGlow:angel?1:0,wingSpread:angel?14:0,hornSplay:0};
  if(angel){eye.sparkle=1;mouth=mouthDefaults({width:54,open:35,corner:2,tongue:1,line:2.5});}
  else {Object.assign(eye,{sclera:1,iris:.62,inward:.6,lidTop:.1,lidAngle:20,lidInk:1});
    mouth=mouthDefaults({width:74,open:30,skew:7.5,tongue:1,fang:1,fangX:.62,shiftX:3,line:4});}
  const f={eyes:[{...eye},{...eye}],brows:[{...brow},{...brow}],mouth,accessories};
  const eyes=values=>f.eyes.forEach(e=>Object.assign(e,values)),brows=values=>f.brows.forEach(b=>Object.assign(b,values));
  if(kind==='resting') {
    if(angel){eyes({sparkle:0,highlight2:1});f.mouth=mouthDefaults({width:42,corner:7,line:2.5});accessories.dashes=.35;accessories.blush=.85;}
    else {eyes({inward:0,lidTop:.24,lidAngle:8});brows({dy:3,angle:-6});f.mouth=mouthDefaults({width:52,corner:6,skew:1,fang:1,fangX:.55,line:4});accessories.motion=0;accessories.blush=.8;}
  } else if(kind==='talking') {
    if(angel){eyes({open:1.08,sparkle:1});brows({dy:-4,arch:1.2});f.mouth=mouthDefaults({width:42,corner:5,tongue:1,line:2.5});accessories.wingSpread=-4;}
    else {eyes({inward:.45,lidTop:.24,lidAngle:20});brows({dy:7});f.mouth=mouthDefaults({width:52,corner:3,skew:4,tongue:1,fang:1,fangX:.55,line:4});}
  } else if(kind==='squished'&&angel) {
    eyes({sclera:1,iris:.55,open:1.12});Object.assign(f.brows[1],{angle:8,arch:.2,dy:2});
    f.mouth=mouthDefaults({width:16,open:20,round:1,tongue:1,line:2.5});accessories.blush=.8;accessories.dashes=.8;
  } else if(kind==='surprised') {
    if(angel){eyes({sclera:1,iris:.5,open:1.2,size:1.08});brows({dy:-8});f.mouth=mouthDefaults({width:25,open:40,round:1,tongue:1,line:2.5});}
    else {eyes({iris:.42,open:1.15,size:1.06,inward:0,lidTop:0,lidAngle:0});brows({dy:-8,angle:-12});f.mouth=mouthDefaults({width:28,open:28,round:1,tongue:1,line:4});}
  } else if(kind==='happy') {
    eyes({closed:1,arcUp:1});brows(angel?{dy:-6}:{dy:-4,angle:-4});
    if(angel){f.mouth=mouthDefaults({width:50,open:28,corner:6,tongue:1,line:2.5});accessories.dashes=0;}
    else f.mouth=mouthDefaults({width:76,open:30,corner:4,skew:4,tongue:1,fang:1,fangX:.6,line:4});
  }
  return f;
}
// FaceParameters.swift:214–319, LipSyncMapping's true openness curve.
function lipMap(c,base,talkOpen,width=.5,rounding=.1) {
  const openness=.13+.72*talkOpen,eased=o=>Math.pow(clamp((o-.14)/.62),.85),knee=.6,full=.85;
  let opening=eased(openness);
  if(openness>knee){const t=clamp((openness-knee)/(full-knee)),v=eased(knee),s=.85*v/(knee-.14)*(full-knee);
    opening=(2*t**3-3*t*t+1)*v+(t**3-2*t*t+t)*s+(-2*t**3+3*t*t);}
  const spread=smooth(.55,.9,width),parting=Math.max(opening,.35*spread),blend=Math.max(smooth(0,.35,parting),spread);
  const rounded=smooth(.3,.75,rounding)*smooth(.1,.3,parting),m={...base,tongue:1};
  if(c==='angel') {
    const narrow=1-rounded*(.25+.3*smooth(.4,.75,parting));
    m.open=34*parting;m.width=mix(42,(48+16*width)*narrow,blend);m.round=rounded*smooth(.45,.7,parting);
  } else {
    const circle=smooth(.55,.8,parting),shallow=smooth(.6,1,width)*(1-rounded);
    m.open=34*parting*(1-.35*shallow)*(1-rounded*(.45-.2*circle));
    m.width=mix(52,(58+22*width)*(1-rounded*(.3+.3*circle)),blend);m.round=rounded*smooth(.3,.6,parting);
  }
  m.corner=base.corner*(1-.3*parting);return m;
}
// InkMouth: nine exact controls, three cubic arcs; bottom-centered tongue.
function mouthPoints(m,c) {
  const d=Math.max(m.open,m.line),g=smooth(c==='angel'?4:5,c==='angel'?12:13,d)*smooth(-2,2,m.corner);
  const w=m.width,lift=m.corner,s=m.skew,k=.5523,left=[-w/2,-lift+s],right=[w/2,-lift-s];
  const top=[left,[-w/6,lift/3+s/3],[w/6,lift/3-s/3],right],q1=[w/6,lift/3-s/3+4*d/3],q2=[-w/6,lift/3+s/3+4*d/3];
  const thin=[...top,mul(add(right,q1),.5),mul(add(add(right,mul(q1,2)),q2),.25),[0,d],mul(add(add(q1,mul(q2,2)),left),.25),mul(add(q2,left),.5)];
  const bowl=[...top,[w/2,right[1]+k*(d-right[1])],[k*w/2,d],[0,d],[-k*w/2,d],[-w/2,left[1]+k*(d-left[1])]];
  const up=Math.max(lift,0),cy=(d-up)/2,ry=(d+up)/2;
  const oval=[[-w/2,cy],[-w/2,cy-4*ry/3],[w/2,cy-4*ry/3],[w/2,cy],[w/2,cy+k*ry],[k*w/2,cy+ry],[0,cy+ry],[-k*w/2,cy+ry],[-w/2,cy+k*ry]];
  return thin.map((p,i)=>add(lerp(lerp(p,bowl[i],g),oval[i],m.round),[m.shiftX,0]));
}
const mouthOutline=p=>new Path().M(p[0]).C(p[1],p[2],p[3]).C(p[4],p[5],p[6]).C(p[7],p[8],p[0]).Z();
function cubicY(a,b,c,d,x) {let lo=0,hi=1;const increasing=d[0]>=a[0];for(let i=0;i<24;i++) {
  const mid=(lo+hi)/2;if((cubic(a,b,c,d,mid)[0]<x)===increasing)lo=mid;else hi=mid;
}return cubic(a,b,c,d,(lo+hi)/2)[1];}
const topY=(p,x)=>cubicY(...p.slice(0,4),x);
const bottomY=(p,x)=>x>=p[6][0]?cubicY(p[3],p[4],p[5],p[6],x):cubicY(p[6],p[7],p[8],p[0],x);
function drawMouth(svg,p) {
  const c=p.character,m={...p.features.mouth},angel=c==='angel',ink=colors[c].ink;
  if(p.squeeze>0){m.width*=1+.2*p.squeeze;m.corner-=6*p.squeeze;}
  const d=Math.max(m.open,m.line),pts=mouthPoints(m,c);
  const anchor=[4*p.turn[0],(angel?25:32.5)+3*p.turn[1]+6*p.squeeze];
  const transform=v=>p.part(add(v,anchor),'mouth',anchor),shape=mouthOutline(pts).map(transform);
  let body=svg.fill(shape,ink),tongueScale=m.tongue*smooth(12,angel?18:20,d+(angel?0:8*m.round));
  if(tongueScale>.01){const small=1-smooth(16,30,d),rx=m.width*((angel?.27:.26)+.1*small+(angel?.07:.08)*m.round)*tongueScale;
    const ry=Math.min(angel?15:14,((angel?.42:.4)+(angel?.23:.25)*small)*d)*tongueScale;
    body+=`<g clip-path="${svg.clip(shape)}">${svg.fill(ellipse([m.shiftX,d],rx,ry).map(transform),'#EE6E4D')}</g>`;}
  body+=svg.stroke(shape,ink,(angel?2.6:3.2)*p.ink,1,'butt');
  const fangScale=clamp(m.fang)*(1-smooth(.25,.75,m.round));
  if(!angel&&fangScale>.02) {
    const x=m.fangX*m.width/2+m.shiftX,half=.127*74/2*fangScale,band=x=>Math.max(0,bottomY(pts,x)-topY(pts,x));
    const open=smooth(m.line,m.line+8,band(x)),root=x=>topY(pts,x)+mix(band(x)+.4*3.2,1,open);
    const height=mix(9,Math.min(13,.6*band(x)),open)*fangScale,left=[x-half,root(x-half)],right=[x+half,root(x+half)],top=(left[1]+right[1])/2;
    const tip=[x+.15*half,top+height],rc=[x+.6*half,top+.6*height],lc=[x-.5*half,top+.5*height];
    const tooth=new Path().M(left).L(right).Q(rc,tip).Q(lc,left).Z();
    const outline=new Path().M(add(left,mul(unit(sub(left,lc)),5))).L(add(right,mul(unit(sub(right,rc)),5))).L(right).Q(rc,tip).Q(lc,left).Z();
    const below=new Path().M(sub(pts[0],[40,0])).L(pts[0]).C(pts[1],pts[2],pts[3]).L(add(pts[3],[40,0])).L(add(pts[3],[40,120])).L(add(pts[0],[-40,120])).Z();
    body+=`<g clip-path="${svg.clip(below.map(transform))}">${svg.stroke(outline.map(transform),ink,2*(3.6-.7)*smooth(0,.5,fangScale)*p.ink,1,'butt')}</g>`;
    body+=svg.fill(tooth.map(transform),'#FFFFFF')+svg.stroke(tooth.map(transform),ink,1.4*p.ink,1,'butt');
  }
  return body;
}
// BrowShape: rotated chord, upward normal, 16-sample tapered devil brow.
function browPath(c,side,b) {
  const o0=c==='angel'?[89,-42]:[97.7,-34],i0=c==='angel'?[61,-58]:[41.8,-13.9];
  const o=[o0[0]*side,o0[1]],i=[i0[0]*side,i0[1]],mid=lerp(o,i,.5),theta=-side*b.angle*deg;
  const a=add(add(mid,rotate(sub(o,mid),theta)),[0,b.dy]),z=add(add(mid,rotate(sub(i,mid),theta)),[0,b.dy]);
  let n=unit([z[1]-a[1],a[0]-z[0]]);if(n[1]>0)n=mul(n,-1);
  const control=add(lerp(a,z,.5),mul(n,2*(c==='angel'?5:2)*b.arch));
  if(c==='angel')return new Path().M(a).Q(control,z);
  const top=[],bottom=[];
  for(let k=0;k<=16;k++){const t=k/16,point=quad(a,control,z,t),tangent=add(mul(sub(control,a),2*(1-t)),mul(sub(z,control),2*t));
    let normal=unit([tangent[1],-tangent[0]]);if(normal[1]>0)normal=mul(normal,-1);
    const half=mix(9.5,4.5,t**1.3)/2;top.push(add(point,mul(normal,half)));bottom.push(sub(point,mul(normal,half)));}
  const away=unit(sub(a,z)),p=new Path().M(top[0]);top.slice(1).forEach(v=>p.L(v));
  p.C(sub(top[16],mul(away,2.25*1.33)),sub(bottom[16],mul(away,2.25*1.33)),bottom[16]);
  bottom.slice(0,-1).reverse().forEach(v=>p.L(v));return p.C(add(bottom[0],mul(away,4.75*1.33)),add(top[0],mul(away,4.75*1.33)),top[0]).Z();
}
function upperLid(rx,ry,lidTop,rotation,weight=7,outline=4.5) {
  const lidY=-ry+2*ry*clamp(lidTop,0,.95),points=[],cut=[];
  for(let k=0;k<=40;k++){const t=Math.PI-.5+(Math.PI+1)*k/40,q=rotate([rx*Math.cos(t),ry*Math.sin(t)],-rotation);
    cut.push(q[1]<lidY);q[1]=Math.max(q[1],lidY);points.push(rotate(q,rotation));}
  const top=points.reduce((best,p,i)=>p[1]<points[best][1]?i:best,0),first=cut.includes(true)?cut.indexOf(true):top,last=cut.includes(true)?cut.lastIndexOf(true):top;
  const above=[],below=[];
  for(let k=0;k<=40;k++){const heavy=k<first?smooth(0,first,k):k>last?smooth(40,last,k):1,half=mix(outline,weight,heavy)/2;
    const d=sub(points[Math.min(k+1,40)],points[Math.max(k-1,0)]),n=unit([d[1],-d[0]]);
    above.push(add(points[k],mul(n,half)));below.push(sub(points[k],mul(n,half)));}
  return poly([...above,...below.reverse()]);
}
function glyph(rx,ry,side,e) {
  const total=Math.max(e.arcUp+e.arcDown+e.chevron,1e-9),wu=e.arcUp/total,wd=e.arcDown/total;
  const up=[[-.9*rx,.15*ry],[0,-.75*ry],[.9*rx,.15*ry]],down=[[-.9*rx,-.1*ry],[0,.7*ry],[.9*rx,-.1*ry]];
  const chev=[[side*.7*rx,-.5*ry],[-side*.6*rx,0],[side*.7*rx,.5*ry]],led=e.chevron/total>=.5,wc=led?mix(.3,1,(e.chevron/total-.5)*2):0;
  const pts=led?chev:up.map((p,i)=>add(mul(p,wu/Math.max(wu+wd,1e-9)),mul(down[i],wd/Math.max(wu+wd,1e-9))));
  const points=Array.from({length:9},(_,i)=>{const t=i/8,line=t<.5?lerp(pts[0],pts[1],2*t):lerp(pts[1],pts[2],2*t-1);return lerp(quad(...pts,t),line,wc);});
  return e.chevron>.5?poly(points,false):smoothPath(points);
}
function starPath(c,r,waist=.28) {
  const tips=Array.from({length:4},(_,i)=>add(c,rotate([0,-r],i*Math.PI/2))),waists=Array.from({length:4},(_,i)=>add(c,rotate([0,-r*waist],Math.PI/4+i*Math.PI/2)));
  const p=new Path().M(tips[0]);for(let i=0;i<4;i++)p.Q(lerp(c,waists[i],.9),tips[(i+1)%4]);return p.Z();
}
// StageRenderer.swift:287–500: clipped ellipse sclera, iris, shade, glyph,
// upper-lid band and highlights; no painted-over field in transparent patches.
function drawEyes(svg,p) {
  const c=p.character,angel=c==='angel',col=colors[c],f=p.features;let body='';
  for(const [index,side] of [-1,1].entries()) {
    const e=f.eyes[index],base=mul(angel?[24,30]:[25,27],e.size),gaze=p.gaze,open=e.open*(1-Math.max(p.blink,e.closed));
    const openAlpha=smooth(.1,.22,open),sclera=clamp(e.sclera),scleraAlpha=smooth(0,.3,sclera);
    const center=add([side*(angel?65:69.5),0],[6*p.turn[0],4*p.turn[1]]),transform=v=>p.part(v,'eyes',center);
    const o=add(center,mul([gaze[0]*5,gaze[1]*4],1-sclera)),rx=base[0]*(1-.08*Math.abs(gaze[0])*(1-sclera)),ry=Math.max(base[1]*open,.01);
    if(openAlpha>.001) {
      const outline=ellipse(o,rx,ry).map(transform),lidY=-ry+2*ry*clamp(e.lidTop,0,.95),rotation=-side*e.lidAngle*deg,ri=e.iris*base[0];
      const irisRadii=[mix(rx,ri,sclera),mix(ry,1.1*ri,sclera)];
      let offset=mul([gaze[0]*(base[0]-ri),gaze[1]*(base[1]-ri)],.85*sclera);
      if(!angel)offset[1]+=.12*ri*sclera;
      if(e.inward>0)offset=add(offset,mul([-side*.8*(base[0]-ri),-.6*(base[1]-ri)],e.inward*(1-.8*Math.abs(gaze[0]))*sclera));
      const irisCenter=add(o,offset),lowerLid=e.lowerLid+(!angel?.06*p.squeeze*p.press:0),lowerY=ry-2*ry*clamp(lowerLid,0,.9),clips=[];
      if(e.lidTop>.001)clips.push(svg.clip(poly([[-3*rx,lidY],[3*rx,lidY],[3*rx,lidY+8*ry],[-3*rx,lidY+8*ry]].map(v=>transform(add(o,rotate(v,rotation)))))));
      if(lowerLid>.001)clips.push(svg.clip(poly([[-3*rx,-8*ry],[3*rx,-8*ry],[3*rx,lowerY],[-3*rx,lowerY]].map(v=>transform(add(o,v))))));
      let eye='';
      if(sclera>.001){eye+=svg.fill(outline,col.sclera,smooth(0,.04,sclera));
        if(!angel){const top=add(o,rotate([0,lidY],rotation)),down=rotate([0,1],rotation);
          eye+=svg.fill(outline,svg.linear([[0,'#F6B987',.75*scleraAlpha],[.4,'#F6B987',.75*scleraAlpha*.45],[1,'#F6B987',0]],transform(top),transform(add(top,mul(down,.5*(ry-lidY))))));}}
      let iris=svg.fill(ellipse(irisCenter,...irisRadii).map(transform),col.ink);
      const squeeze=Math.min(1,Math.max(open,.2)),counter=mul(gaze,-1.5),starry=smooth(.35,.65,e.starry),star=starry*(1-scleraAlpha),alpha=smooth(.3,.7,e.highlight)*(1-star);
      if(alpha>.001){const beadSize=angel?[14,19]:[14,20],beadOffset=angel?[-side*7.5,-12]:[10,-6];
        const dotOffset=angel?[-side*.35*ri,-.35*ri]:[side*.33*ri,-.3*ri],beadCentre=add(o,[beadOffset[0],beadOffset[1]*squeeze]);
        const centre=add(lerp(beadCentre,add(irisCenter,dotOffset),sclera),counter),dot=(angel?.6:.42)*ri,size=lerp(beadSize,[dot,dot],sclera);
        iris+=svg.fill(ellipse(centre,size[0]/2,size[1]/2*squeeze).map(transform),col.highlight,alpha);}
      if(e.highlight2>.001&&angel)iris+=svg.fill(ellipse(add(add(o,[side*6,8*squeeze]),counter),3,3*squeeze).map(transform),col.highlight,e.highlight2*(1-scleraAlpha));
      if(e.highlight2>.001&&!angel&&sclera>.001){const r=.12*ri*Math.min(1,e.highlight2);iris+=svg.fill(ellipse(add(add(irisCenter,[-side*.34*ri,.36*ri]),counter),r,r*squeeze).map(transform),col.highlight,scleraAlpha);}
      const sparkle=smooth(.2,.5,e.sparkle)*(1-starry)*(1-scleraAlpha);
      if(sparkle>.001&&angel)iris+=svg.fill(starPath(add(add(o,[side*8,13*squeeze]),counter),7.5*squeeze,.28).map(transform),col.highlight,sparkle);
      if(star>.001)iris+=svg.fill(starPath(irisCenter,.55*irisRadii[0]*squeeze,.3).map(transform),col.highlight,star);
      eye+=sclera>.001?`<g clip-path="${svg.clip(outline)}">${iris}</g>`:iris;
      if(sclera>.001)eye+=svg.stroke(outline,col.ink,4.5*p.ink*smooth(0,.15,sclera),1,'butt');
      eye=clips.reduceRight((s,clip)=>`<g clip-path="${clip}">${s}</g>`,eye);
      body+=`<g opacity="${openAlpha}">${eye}</g>`;
      if(lowerLid>.001){const half=rx*Math.sqrt(Math.max(0,1-(lowerY/ry)**2));body+=svg.stroke(new Path().M(add(o,[-half-2,lowerY])).Q(add(o,[0,lowerY+2]),add(o,[half+2,lowerY])).map(transform),col.ink,3*p.ink,openAlpha);}
      if(!angel&&sclera>.001)body+=svg.fill(upperLid(rx,ry,e.lidTop,rotation,mix(4.5,7,clamp(e.lidInk))).map(v=>transform(add(o,v))),col.ink,scleraAlpha*openAlpha);
    }
    if(openAlpha<.999)body+=svg.stroke(glyph(...base,side,e).map(v=>transform(add(center,v))),col.ink,(angel?6.5:7)*p.ink,1-openAlpha);
    const b={...f.brows[index]};b.dy+=(angel?6:3)*p.squeeze+5*p.turn[1];b.angle+=8*p.squeeze;
    const mid=angel?[side*75,-50]:[side*69.75,-23.95],bp=browPath(c,side,b).map(v=>p.part(add(v,[6*p.turn[0],0]),'brows',mid));
    body+=angel?svg.stroke(bp,col.ink,6*p.ink,b.alpha):svg.fill(bp,col.ink,b.alpha);
  }
  return body;
}
const wingSegments=[
  [[-167.5,-18],[-156.5,-18],[-143,-3],[-137,6]], [[-137,6],[-129,18],[-122,33.5],[-122,48]],
  [[-122,48],[-122,62],[-131,72],[-145,72]], [[-145,72],[-152.5,72],[-164.5,68.5],[-164.5,59.5]],
  [[-164.5,59.5],[-164.5,53.5],[-160.5,53],[-158,50]], [[-158,50],[-167,47],[-178,39],[-178,28.5]],
  [[-178,28.5],[-178,18.5],[-168.5,20.5],[-165.5,19]], [[-165.5,19],[-171.5,12.5],[-177.5,3.5],[-177.5,-5.5]],
  [[-177.5,-5.5],[-177.5,-12],[-174.5,-18],[-167.5,-18]]];
const wingPath=()=>{const p=new Path().M(wingSegments[0][0]);wingSegments.forEach(([,a,b,d])=>p.C(a,b,d));return p.Z();};
// HornShape, including the 16 nested shine bands from arc-length samples.
function hornGeometry() {
  const outer=[-135.5,-47.1],inner=[-102,-71.9],bulge=[-147.5,-80],tip=[-116,-117],r=1.6;
  const backDir=unit([-.54,-.84]),capEnd=add(tip,[2.8,5.5]),capDir=unit([-.1,1]),innerDir=unit([.63,.78]);
  const start=add(outer,mul(backDir,r)),back=[[start,add(start,mul(backDir,12)),add(bulge,[0,13]),bulge],[bulge,add(bulge,[0,-18]),add(tip,[-15,3]),tip]];
  const along=unit(sub(outer,inner)),innerEnd=sub(inner,mul(innerDir,r)),path=new Path().M(start);
  back.forEach(([,a,b,d])=>path.C(a,b,d));path.C(add(tip,[2,-.4]),sub(capEnd,mul(capDir,2.5)),capEnd)
    .C(add(capEnd,mul(capDir,20)),sub(innerEnd,mul(innerDir,15)),innerEnd).Q(inner,add(inner,mul(along,r))).L(sub(outer,mul(along,r))).Q(outer,start).Z();
  const samples=[];let distance=0;
  for(const segment of back)for(let k=0;k<=64;k++){if(!k&&samples.length)continue;const point=cubic(...segment,k/64);if(samples.length)distance+=length(sub(point,samples.at(-1).point));samples.push({point,distance});}
  function backPoint(u){const target=clamp(u)*distance;let i=samples.findIndex(p=>p.distance>=target);i=clamp(i<0?samples.length-1:i,1,samples.length-1);
    const a=samples[i-1],b=samples[i],t=(target-a.distance)/Math.max(b.distance-a.distance,1e-12),d=sub(b.point,a.point);return {point:lerp(a.point,b.point,clamp(t)),normal:unit([-d[1],d[0]])};}
  const strength=v=>Math.pow(Math.sin(Math.PI*Math.pow(clamp(v),.85)),.8),alongShine=Array.from({length:41},(_,k)=>({v:k/40,...backPoint(mix(.32,.9,k/40))}));
  const shines=Array.from({length:16},(_,k)=>{const level=k/16,outerSide=[],innerSide=[],present=alongShine.map(p=>strength(p.v)>level);
    alongShine.forEach(({v,point,normal},i)=>{if(!(present[i]||present[i-1]||present[i+1]))return;let outer=5.1,inner=5.1;
      if(present[i]){const t=Math.acos(clamp(2*level/strength(v)-1,-1,1))/Math.PI,taper=Math.pow(Math.sin(Math.PI*v),.6);outer=5.1-(1.9+1.3*t)*taper;inner=5.1+(1.9+2.6*t)*taper;}
      outerSide.push(add(point,mul(normal,outer)));innerSide.push(add(point,mul(normal,inner)));});return poly([...outerSide,...innerSide.reverse()]);});
  return {path,shines};
}
const horn=hornGeometry();
// FacePose/FaceDrawing: volume-preserving head and part counter-scale.
function pose(character,options={}) {
  const p={character,features:expression(character,options.expression||'neutral'),anchor:[206,238],scale:412/400,sy:1,tilt:0,gaze:[0,0],blink:0,press:0,...options};
  p.sx=Math.pow(p.sy,-.4);p.squeeze=clamp((1-p.sy)/(character==='devil'?.30:.12));p.turn=[.5*p.gaze[0],.4*p.gaze[1]];
  p.ink=p.scale*Math.sqrt(p.sx*p.sy);
  p.point=v=>add(p.anchor,rotate([v[0]*p.scale*p.sx,v[1]*p.scale*p.sy],p.tilt));
  p.part=(v,part,center)=>{const k={eyes:character==='angel'?1:.5,brows:.4,mouth:.3}[part];return p.point(add(center,[v[0]-center[0],(v[1]-center[1])*Math.pow(p.sy,-k)]));};
  return p;
}
function drawAccessories(svg,p,{glow=true}={}) {
  const c=p.character,angel=c==='angel',f=p.features,a=f.accessories,col=colors[c];let body='';
  for(const side of [-1,1]) {
    if(angel) {
      const hinge=[-127,52],at=p.point([-side*hinge[0],hinge[1]]),angle=p.tilt+side*(a.wingSpread-25*p.squeeze)*deg,k=1-.15*p.squeeze;
      const transform=v=>add(at,rotate([(v[0]-hinge[0])*-side*p.scale*Math.pow(p.sx,.5)*k,(v[1]-hinge[1])*p.scale*Math.pow(p.sy,.5)*k],angle));
      body+=svg.fill(wingPath().map(transform),svg.linear([[0,'#FDFBE9',.94],[1,'#FBF5D7',.9]],transform(hinge),transform([-170,-12])));
    } else {
      const hinge=[-118.75,-59.5],at=p.point([-side*hinge[0],hinge[1]+26*p.squeeze*p.press]),angle=p.tilt+side*(a.hornSplay+(p.press?16+18*p.squeeze:0))*deg;
      const affine=`translate(${at}) rotate(${angle/deg}) scale(${-side*p.scale*Math.pow(p.sx,.3)},${p.scale*Math.pow(p.sy,.3)}) translate(${-hinge[0]},${-hinge[1]})`;
      const fill=svg.radial([[0,'#6E1F12'],[.25,'#58190F'],[.5,'#3E120B'],[.75,'#2F0D07'],[1,'#2A0B06']],[-106,-63.9],58,58);
      body+=`<g transform="${affine}">${svg.fill(horn.path,fill)}<g clip-path="${svg.clip(horn.path)}">${horn.shines.map(band=>svg.fill(band,'#D65A30',.09)).join('')}</g></g>`;
    }
  }
  let amount=a.blush;if(!angel)amount=Math.min(1,amount*(1+.6*Math.max(0,p.puff||0)));
  const size=angel?[39.5,24]:[38,24],blushScale=a.blushSize*(1+.25*p.squeeze),puff=angel?0:p.puff||0;
  const rx=size[0]*blushScale/2*(1+.4*puff)*p.scale*p.sx,ry=size[1]*blushScale/2*(1+.12*puff)*p.scale*p.sy,reach=angel?1.15:1.2;
  for(const side of [-1,1]) {
    const center=p.point(add([side*(angel?86.5:88),angel?46:42],[3*p.turn[0],2*p.turn[1]+4*p.squeeze]));
    const stops=angel?[[0,col.blush,.75*amount],[.55,col.blush,.75*amount*.9],[1,col.blush,0]]:
      [[0,1],[.45,.97],[.62,.86],[.74,.62],[.84,.33],[.92,.12],[1,0]].map(([t,alpha])=>[t,col.blush,.46*amount*alpha]);
    body+=`<g transform="translate(${center}) rotate(${p.tilt/deg})">${svg.fill(ellipse([0,0],rx*reach,ry*reach),svg.radial(stops,[0,0],rx*reach,ry*reach))}</g>`;
    const marks=angel?[[[124,-119.5],24,8,46],[[132,-88.5],25,8,22]]:[[[147,-33],30,9.5,40],[[157,-4],30,9.5,6]];
    const intensity=angel?a.dashes:a.motion,k=Math.min(1.45,intensity);
    if(intensity>.01)for(const [center,length,thickness,angle] of marks) {
      const at=[side*center[0],center[1]],direction=[-side*Math.cos(angle*deg),Math.sin(angle*deg)],half=(length-thickness)/2*k;
      body+=svg.stroke(new Path().M(sub(at,mul(direction,half))).L(add(at,mul(direction,half))).map(p.point),angel?'#FFFDF0':'#FFCE4F',thickness*k*p.scale,Math.min(1,intensity)*(angel?.95:1));
    }
  }
  if(angel) {
    const centre=p.point(add([0,-115.2],[-3*p.turn[0],-2*p.turn[1]])),squash=(1-.3*(1-p.sy))*(1-.25*p.squeeze);
    const ring=ellipse([0,0],93.5,25.5).append(ellipse([0,0],75.5,13.5));let halo='';
    if(glow&&a.haloGlow>.01){
      // GlowSprites: zero-offset blur12fu/24fu passes plus a crisp ring.
      // Cairo Gaussian vs CoreGraphics shadow kernel is a documented backend difference.
      const blur=sd=>svg.def(id=>`<filter id="${id}" x="-40%" y="-100%" width="180%" height="300%" color-interpolation-filters="sRGB"><feGaussianBlur stdDeviation="${sd}"/></filter>`);
      const extent=poly([[-111.5,-43.5],[111.5,-43.5],[111.5,43.5],[-111.5,43.5]]);
      // Source shadow alpha multiplies the .85-alpha ring, and its bitmap is
      // clipped to outerSize+3*blurFu =223x87fu (GlowSprites.extent).
      const bloom=svg.fill(ring,'#FFD447',.85*.85*a.haloGlow,`fill-rule="evenodd" filter="${blur(6)}"`)
        +svg.fill(ring,'#FFD447',.85*.85*.6*a.haloGlow,`fill-rule="evenodd" filter="${blur(12)}"`);
      halo+=`<g clip-path="${svg.clip(extent)}">${bloom}</g>`;
    }
    halo+=svg.fill(ring,'#FFFDF4',1,'fill-rule="evenodd"');
    body+=`<g transform="translate(${centre}) rotate(${p.tilt/deg}) scale(${p.scale},${p.scale*squash})">${halo}</g>`;
  }
  return body;
}
function drawField(svg,p,width=412) {
  const angel=p.character==='angel',center=p.point([0,angel?-120:-50]);
  return `<rect width="412" height="412" fill="${svg.radial(field[p.character],center,width*(angel?1:1.04),width*(angel?1.05:.85))}"/>`;
}
function render(p,part='all',options={}) {
  const svg=new SVG();let body='';
  if(['all','faceplate','field'].includes(part))body+=drawField(svg,p,options.fieldWidth||412);
  if(['all','faceplate','accessories'].includes(part))body+=drawAccessories(svg,p,options);
  if(part==='all'||part==='eyes')body+=drawEyes(svg,p);
  if(part==='all'||part==='mouth')body+=drawMouth(svg,p);
  return svg.wrap(body);
}
function pack(character) {
  const base=pose(character),layers={eyes:{frames:{}},mouth:{frames:{}}};
  for(const key of ['open','half','closed','look-left','look-right','happy','neutral']) {
    // Angel white eyes use the app's surprised parameters; source neutral
    // remains separately available as bead eyes for honest comparisons.
    const p=pose(character,{expression:key==='happy'?'happy':key==='neutral'?'neutral':character==='angel'?'surprised':'talking',
      gaze:character==='devil'&&key!=='neutral'&&key!=='happy'?[-.2,-.8]:[0,0]});
    if(key==='half')p.blink=.5;if(key==='closed')p.blink=1;
    if(key==='look-left'||key==='look-right'){p.gaze=[key==='look-left'?-.85:.85,.1];p.turn=[.5*p.gaze[0],.4*p.gaze[1]];}
    layers.eyes.frames[key]=render(p,'eyes');
  }
  for(const [key,level] of [['rest-smile',null],['small',.25],['medium',.5],['wide',.75],['round',1]]) {
    const p=pose(character,{expression:level===null?'neutral':'talking'});
    if(level!==null)p.features.mouth=lipMap(character,p.features.mouth,level);
    layers.mouth.frames[key]=render(p,'mouth');
  }
  return {base:`${character}-faceplate-412.png`,palette:{...colors[character],tongue:'#EE6E4D',fang:'#FFFFFF'},
    faceplate:render(base,'faceplate'),layers,source:{repository:'https://github.com/pipecat-ai/ios-little-voices',commit:'7f5613cd9071d4668d3aab9cfe8691ff8c59b4b2',
      renderer:'../little-voices.cjs',layout:{scale:base.scale,anchor:base.anchor,stageWidthFu:400},
      eyesOpen:character==='angel'?'ExpressionLibrary.surprised (white sclera)':'ExpressionLibrary.talking + CharacterDirector.speakerLookAtOther',
      mouthOpenLevels:[.25,.5,.75,1],mouthRoundName:'Legacy round key now stores maximum talkOpen; not a rounded viseme. Devil keeps its fang.'},
    integration:{audio:'Rest is app neutral open smile/grin. Speaking levels use LipSyncMapping at talkOpen .25/.5/.75/1, width .5, rounding .1.',
      timing:'20 Hz; blink closure .5/1 uses app geometry. Energy drive is not phoneme alignment.',
      rendering:'Restore dirty union from gradient faceplate; redraw intersecting layers. Straight RGBA; animated patches contain no field.',
      status:'Settled Little Voices renderer port. Runtime estimates and user art approval pending.'}};
}
module.exports={Path,SVG,ellipse,poly,pose,expression,lipMap,mouthPoints,mouthOutline,drawEyes,drawMouth,drawField,drawAccessories,render,pack,field,upperLid,browPath};
