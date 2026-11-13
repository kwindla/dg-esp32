(() => {
  'use strict';
  const data=window.HalloweenSprites, canvas=document.querySelector('canvas'), ctx=canvas.getContext('2d');
  const $=id=>document.getElementById(id);
  const ui={listen:$('listen'),speak:$('speak'),demo:$('demo'),stop:$('stop'),label:$('listen-label'),
    feedback:$('feedback'),caption:$('caption'),badge:$('state-badge'),state:$('state-name'),
    meter:$('energy-fill'),meterLabel:$('meter-label'),display:$('display'),line:$('line'),
    quickListen:$('quick-listen'),quickSpeak:$('quick-speak')};
  const base=new Image(), synth=window.speechSynthesis;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)');
  let ready=false,mode='demo',generation=0,startedAt=performance.now(),frames={},energy=0;
  let stream=null,audio=null,source=null,analyser=null,samples=null,utterance=null;
  let speechStarted=0,lastBoundary=0,watchdog=0,lastFrame=0,nextBlink=performance.now()+2600,blinkAt=-10000;
  let lastState='',lastCaption='',lastVoice=0;
  function revealCat(){if(innerWidth<=740)ui.display.scrollIntoView({behavior:reduced.matches?'instant':'smooth',block:'center'});}
  function feedback(text,error=false){ui.feedback.textContent=text;ui.feedback.dataset.error=String(error);}
  function caption(text){if(text!==lastCaption){ui.caption.textContent=text;lastCaption=text;}}
  function releaseMic(){
    if(stream)stream.getTracks().forEach(track=>track.stop());stream=null;
    if(source)source.disconnect();source=null;analyser=null;samples=null;
    if(audio){audio.close().catch(()=>{});audio=null;}
  }
  function stopCurrent(){
    generation++;clearTimeout(watchdog);watchdog=0;releaseMic();
    const old=utterance;utterance=null;
    if(old&&synth)synth.cancel();speechStarted=0;lastBoundary=0;energy=0;
  }
  function setMode(next){
    mode=next;startedAt=performance.now();lastState='';
    ui.listen.setAttribute('aria-pressed',String(next==='listening'||next==='requesting'));
    ui.demo.setAttribute('aria-pressed',String(next==='demo'));
    ui.label.textContent=next==='listening'?'Stop listening':next==='requesting'?'Cancel microphone request':'Listen to me';
    ui.speak.textContent=next==='speaking'||next==='speech-pending'?'Stop talking':'♪ Hear the cat';
    ui.quickListen.textContent=next==='listening'?'◉ Stop listening':next==='requesting'?'Cancel mic request':'◉ Listen';
    ui.quickSpeak.textContent=next==='speaking'||next==='speech-pending'?'■ Stop talking':'♪ Talk';
    ui.stop.hidden=next==='idle'||next==='demo';
  }
  function rest(message='The cat is resting. Try listening or give it a line.'){
    stopCurrent();setMode('idle');caption('A little catnap between spells.');feedback(message);
  }
  function startDemo(message='A looping listening-and-talking demo. Sound is off.'){
    stopCurrent();setMode('demo');feedback(message);
  }
  function render(next){
    const names=Object.keys(data.layers);
    for(const name of names)if(next[name]!==frames[name]){
      const [x,y,w,h]=data.layers[name].bounds;ctx.drawImage(base,x,y,w,h,x,y,w,h);
    }
    for(const name of names)for(const [x,y,w,h,key]of data.layers[name].frames[next[name]]){
      ctx.fillStyle=data.palette[key];ctx.fillRect(x*4,y*4,w*4,h*4);
    }
    frames={...next};
  }
  function mouthFor(value,now){
    if(value<.08)return 'closed';if(value<.3)return 'small';
    if(value<.6)return 'medium';return Math.floor(now/150)%4===0?'round':'wide';
  }
  function update(now){
    let state=mode,value=0;
    const elapsed=now-startedAt;
    if(mode==='demo'){
      const phase=elapsed%12500;
      state=phase<5500?'listening':phase<10500?'speaking':'idle';
      if(state==='listening')value=Math.max(0,Math.sin(phase*.006)*.6+Math.sin(phase*.013)*.3);
      if(state==='speaking'){
        const rhythm=[0,.35,.8,1,.6,.25,0,.75,.5,0,.4,.9];
        value=rhythm[Math.floor(phase/110)%rhythm.length];
      }
      caption(state==='listening'?'Go on. Tell me your spooky story.':state==='speaking'?"A haunted house? I'd rather haunt the treat cupboard.":'Your turn, little trick-or-treater.');
    }else if(mode==='listening'&&analyser){
      analyser.getFloatTimeDomainData(samples);
      let sum=0;for(const sample of samples)sum+=sample*sample;
      value=Math.min(1,Math.max(0,(Math.sqrt(sum/samples.length)-.008)*16));
      if(value>.13)lastVoice=now;
      caption(value>.13?"I'm listening. What happens next?":'I’m all ears. Say something!');
    }else if(mode==='speaking'){
      const rhythm=[.35,.8,1,.4,.7,.15,.65,.9,.2];
      value=rhythm[Math.floor((now-speechStarted)/95)%rhythm.length];
      // Browser speech has no exposed PCM stream. Word boundary events nudge
      // the rhythm; start/end events gate it to the utterance's lifetime.
      if(lastBoundary&&now-lastBoundary<100)value=Math.max(value,.75);
      caption(utterance?.text||ui.line.value.trim());
    }else if(mode==='requesting'){state='idle';caption('One moment. Let me hear you.');}
    else if(mode==='speech-pending'){state='idle';caption('Warming up my little voice…');}
    energy+=(value-energy)*.45;
    let eyes=state==='listening'?'open':'happy',whiskers='rest',mouth=state==='speaking'?mouthFor(energy,now):'smile';
    if(state==='listening'){
      if(energy>.2)eyes=Math.floor(now/650)%3===0?'left':Math.floor(now/650)%3===1?'open':'right';
      else if(!reduced.matches&&Math.floor(elapsed/1800)%3===1)eyes='left';
      if(energy>.28)whiskers=Math.floor(now/120)%2===0?'lift':'drop';
      else if(now-lastVoice<180)whiskers='drop';
    }else if(!reduced.matches&&now%5200>4950)whiskers='lift';
    if(!reduced.matches){
      if(now>=nextBlink){blinkAt=now;nextBlink=now+2800+Math.random()*2700;}
      const age=now-blinkAt;if(age<160)eyes=age<40||age>=120?'half':'closed';
    }
    if(reduced.matches&&mode==='demo'){eyes=state==='listening'?'open':'happy';whiskers='rest';mouth=state==='speaking'?'small':'smile';}
    render({eyes,mouth,whiskers});
    ui.meter.style.width=`${Math.round(energy*100)}%`;ui.display.style.setProperty('--energy',energy.toFixed(2));
    if(state!==lastState){
      ui.badge.dataset.state=state;
      ui.state.textContent=state==='listening'?(mode==='demo'?'Listening · demo':'Listening to you'):state==='speaking'?(mode==='demo'?'Talking · demo':'Talking to you'):'Taking a little rest';
      ui.meterLabel.textContent=mode==='demo'?'ANIMATION DEMO · SOUND OFF':mode==='listening'?'YOUR VOICE':mode==='speaking'?'THE CAT’S VOICE':'READY WHEN YOU ARE';
      lastState=state;
    }
  }
  function loop(now){if(ready&&now-lastFrame>=50){update(now);lastFrame=now;}requestAnimationFrame(loop);}
  async function listen(){
    if(mode==='listening'||mode==='requesting'){rest('Microphone off.');return;}
    stopCurrent();setMode('requesting');revealCat();feedback('Allow microphone access in your browser, then talk to the cat.');
    const ticket=generation;
    if(!navigator.mediaDevices?.getUserMedia){
      startDemo('Microphone access is unavailable here. Open this HTTPS page in Safari or Chrome, or watch the demo.');return;
    }
    let localAudio;
    try{
      const Audio=window.AudioContext||window.webkitAudioContext;
      if(!Audio)throw new Error('audio-unavailable');
      localAudio=new Audio();audio=localAudio;
      // Resume synchronously from the button gesture, including on iOS.
      await localAudio.resume();
      if(ticket!==generation)return;
      const mic=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(ticket!==generation){mic.getTracks().forEach(t=>t.stop());localAudio.close().catch(()=>{});return;}
      stream=mic;
      analyser=localAudio.createAnalyser();analyser.fftSize=512;
      samples=new Float32Array(analyser.fftSize);source=localAudio.createMediaStreamSource(mic);source.connect(analyser);
      mic.getAudioTracks().forEach(track=>track.addEventListener('ended',()=>{if(mode==='listening')rest('The microphone disconnected. Tap Listen to me to reconnect.');}));
      setMode('listening');feedback('Talk naturally. The eyes and whiskers follow your voice. Tap again to turn the microphone off.');
    }catch(error){
      if(ticket!==generation)return;
      const message=error.name==='NotAllowedError'?'Microphone permission was denied. You can still watch the demo.':error.name==='NotFoundError'?'No microphone was found. You can still watch the demo.':'The microphone could not start. Try your browser’s microphone settings, or watch the demo.';
      startDemo(message);ui.feedback.dataset.error='true';
    }
  }
  function speak(){
    if(mode==='speaking'||mode==='speech-pending'){rest('The cat stopped talking.');return;}
    const text=ui.line.value.trim();
    if(!text){feedback('Give the cat a line to say first.',true);ui.line.focus();return;}
    stopCurrent();
    if(!synth||!window.SpeechSynthesisUtterance){startDemo('This browser has no speech voice. Watch the talking animation in demo mode instead.');return;}
    setMode('speech-pending');revealCat();feedback('Starting the cat’s voice…');
    const ticket=generation,u=new SpeechSynthesisUtterance(text);utterance=u;
    const voices=synth.getVoices(),voice=voices.find(v=>v.lang.startsWith('en')&&v.localService)||voices.find(v=>v.lang.startsWith('en'));
    if(voice)u.voice=voice;u.lang=voice?.lang||'en-US';u.rate=.95;u.pitch=1.2;
    u.onstart=()=>{
      if(ticket!==generation)return;clearTimeout(watchdog);speechStarted=performance.now();
      setMode('speaking');feedback('The cat is speaking your line. Tap Stop talking to interrupt.');
      watchdog=setTimeout(()=>{if(ticket===generation)rest('The cat finished its turn.');},60000);
    };
    u.onboundary=()=>{if(ticket===generation)lastBoundary=performance.now();};
    u.onpause=()=>{if(ticket===generation){setMode('speech-pending');feedback('The voice paused. Tap Stop talking and try again.');}};
    u.onresume=()=>{if(ticket===generation){speechStarted=performance.now();setMode('speaking');}};
    u.onend=()=>{if(ticket===generation)rest('Your turn! Tap Listen to me and tell the cat something.');};
    u.onerror=()=>{if(ticket===generation){startDemo('Your browser’s speech voice could not start. The animation demo is still available.');ui.feedback.dataset.error='true';}};
    watchdog=setTimeout(()=>{if(ticket===generation)startDemo('The voice did not start. Try Hear the cat again, or keep watching the demo.');},7000);
    try{synth.cancel();synth.speak(u);}catch(error){if(ticket===generation)startDemo('The voice could not start. Watch the demo instead.');}
  }
  ui.listen.addEventListener('click',listen);ui.speak.addEventListener('click',speak);
  ui.quickListen.addEventListener('click',listen);ui.quickSpeak.addEventListener('click',speak);
  ui.demo.addEventListener('click',()=>{startDemo();revealCat();});ui.stop.addEventListener('click',()=>rest());
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&mode!=='demo'&&mode!=='idle')rest('Microphone and voice stopped while the app is in the background.');});
  window.addEventListener('pagehide',stopCurrent);
  base.onload=()=>{
    ready=true;ctx.imageSmoothingEnabled=false;ctx.drawImage(base,0,0,412,412);
    [ui.listen,ui.speak,ui.demo,ui.quickListen,ui.quickSpeak].forEach(b=>b.disabled=false);startDemo();
    window.halloweenApp={get mode(){return mode;},get frames(){return {...frames};},get energy(){return energy;},get micActive(){return !!stream;},get ready(){return ready;}};
  };
  base.onerror=()=>feedback('The cat artwork could not load. Refresh this page to try again.',true);
  base.src=data.base;requestAnimationFrame(loop);
})();
