/* Browser simulation. No network, microphone, or third-party dependencies. */
(() => {
  const data=window.HalloweenSprites, canvas=document.querySelector('canvas'), ctx=canvas.getContext('2d');
  const base=new Image(), controls=Object.fromEntries(['eyes','mouth','whiskers','level','paused'].map(id=>[id,document.getElementById(id)]));
  const motion=matchMedia('(prefers-reduced-motion: reduce)');
  controls.paused.checked=motion.matches;
  let mode='idle',nextBlink=3200,blinkAt=-10000,lastTick=-Infinity,clock=0,previous=null,manualBlink=false;
  let frames={eyes:'happy',mouth:'smile',whiskers:'rest'};
  function paint(next) {
    // Restore only the changed feature regions. Whiskers overlap eyes/mouth in
    // y, so compose every layer after restoration to preserve overlaps.
    const dirty=[];
    for(const name of Object.keys(data.layers)) if(next[name]!==frames[name]) dirty.push(data.layers[name].bounds);
    for(const [x,y,w,h] of dirty) ctx.drawImage(base,x,y,w,h,x,y,w,h);
    for(const [name,frame] of Object.entries(next)) for(const [x,y,w,h,key] of data.layers[name].frames[frame]) {
      ctx.fillStyle=data.palette[key];ctx.fillRect(x*4,y*4,w*4,h*4);
    }
    frames={...next};
    document.getElementById('status').textContent=`${mode} · eyes: ${frames.eyes} · mouth: ${frames.mouth} · whiskers: ${frames.whiskers}`;
  }
  function stateAt(t) {
    if(t>=nextBlink) {blinkAt=t;nextBlink=t+3000+Math.random()*3000;}
    const age=t-blinkAt;
    let eyes=mode==='listening'?'open':'happy';
    if(age>=0&&age<160) eyes=age<40||age>=120?'half':'closed';
    if(manualBlink&&age>=160) manualBlink=false;
    let mouth='smile';
    if(mode==='speaking') {
      // Simulated syllables with actual pauses; not phoneme/viseme lip sync.
      const phase=Math.floor(t/100)%16;
      const rhythm=[0,.25,.65,1,.65,.3,0,.35,.8,.5,0,0,.3,.7,.25,0];
      const level=Number(controls.level.value)/100*rhythm[phase];
      mouth=level<.06?'closed':level<.22?'small':level<.5?'medium':phase%8===0?'round':'wide';
    }
    const twitch=t%5100;
    let whiskers=twitch>4200&&twitch<4320?'lift':twitch>=4320&&twitch<4420?'drop':'rest';
    if(controls.paused.checked&&!manualBlink) {eyes=mode==='listening'?'open':'happy';mouth=mode==='speaking'?'closed':'smile';whiskers='rest';}
    const next={eyes,mouth,whiskers};
    for(const name of Object.keys(next)) if(controls[name].value!=='auto') next[name]=controls[name].value;
    return next;
  }
  function tick(now) {
    if(previous!==null&&(!controls.paused.checked||manualBlink)) clock+=Math.min(now-previous,100);
    previous=now;
    if(now-lastTick>=50) {paint(stateAt(clock));lastTick=now;}
    requestAnimationFrame(tick);
  }
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{
    mode=button.dataset.mode;
    document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
  }));
  controls.level.addEventListener('input',()=>document.getElementById('level-value').textContent=`${controls.level.value}%`);
  document.getElementById('blink').addEventListener('click',()=>{controls.eyes.value='auto';blinkAt=clock;manualBlink=true;});
  motion.addEventListener('change',e=>{controls.paused.checked=e.matches;});
  base.onload=()=>{
    ctx.imageSmoothingEnabled=false;ctx.drawImage(base,0,0,412,412);
    frames={eyes:null,mouth:null,whiskers:null};
    paint(stateAt(clock));requestAnimationFrame(tick);
    window.halloweenPreview={data,get frames(){return {...frames};},
      render(eyes,mouth,whiskers){paint({eyes,mouth,whiskers});}};
  };
  base.onerror=()=>document.getElementById('status').textContent='Could not load the face plate. Keep preview.html and its companion files together.';
  base.src=data.base;
})();
