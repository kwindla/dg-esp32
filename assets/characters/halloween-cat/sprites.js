/* Pixel rectangles are [x, y, width, height, palette key] on a 103px grid.
 * The browser preview and PNG exporter consume the same data. */
(function (root) {
  const ink = (x, y, w, h) => [x, y, w, h, 'ink'];
  const pair = (rects) => rects.concat(rects.map(([x,y,w,h,c]) => [101-x-w,y,w,h,c]));
  const happy = pair([ink(40,45,4,1), ink(39,46,2,1)]);
  const open = pair([ink(41,44,2,5), ink(40,45,4,3), [41,45,1,1,'cream']]);
  const half = pair([ink(40,47,4,2), [41,47,1,1,'cream']]);
  const closed = pair([ink(39,47,5,1)]);
  const smile = [ink(43,49,1,2),ink(56,49,1,2),ink(44,51,3,1),
    ink(53,51,3,1),ink(46,52,8,1),ink(49,49,2,2),ink(48,50,4,2)];
  // Keep the raised cat-smile corners and nose stem as the jaw opens.
  // Taper each scanline into a round bowl instead of stretching a box.
  const smileTop = [ink(43,49,1,1),ink(56,49,1,1),ink(43,50,3,1),
    ink(54,50,3,1),ink(49,49,2,1),ink(48,50,4,1)];
  const bowl = (rows,tongue=[]) => smileTop.concat(
    rows.map(([x,y,w])=>ink(x,y,w,1)),tongue);
  const smallMouth = bowl([[44,51,12],[45,52,10],[47,53,6]]);
  const mediumMouth = bowl([[44,51,12],[44,52,12],[45,53,10],[47,54,6]],
    [[49,52,2,1,'tongue'],[48,53,4,1,'tongue']]);
  const wideMouth = bowl([[43,51,14],[43,52,14],[44,53,12],
    [44,54,12],[45,55,10],[47,56,6]],
    [[49,54,2,1,'tongue'],[48,55,4,1,'tongue']]);
  const roundMouth = [[48,49,4],[47,50,6],[46,51,8],[46,52,8],
    [46,53,8],[47,54,6],[48,55,4]].map(([x,y,w])=>ink(x,y,w,1));
  const whiskers = (dy) => pair([
    ink(25,47+dy,4,1),ink(29,48,4,1),ink(33,49,3,1),
    ink(24,50+dy,5,1),ink(29,50,7,1),
    ink(25,54+dy,4,1),ink(29,53,4,1),ink(33,52,3,1)
  ]);
  const data = {
    version: 2,
    display: {width:412,height:412,grid:103,pixelScale:4,fps:20},
    base: 'halloween-cat-faceplate-412.png',
    palette: {ink:'#10051e',cream:'#fff7d9',tongue:'#f17991'},
    layers: {
      eyes: {bounds:[152,176,100,24],frames:{happy,open,half,closed,
        left:open.map(([x,y,w,h,c])=>[x-1,y,w,h,c]),
        right:open.map(([x,y,w,h,c])=>[x+1,y,w,h,c])}},
      mouth: {bounds:[168,192,64,40],frames:{
        smile, closed:smile,
        small:smallMouth, medium:mediumMouth, wide:wideMouth, round:roundMouth
      }},
      whiskers: {bounds:[92,180,224,48],frames:{rest:whiskers(0),lift:whiskers(-1),drop:whiskers(1)}}
    },
    integration: {
      audio:'Drive mouth openness from smoothed PCM RMS at audible playout, gated by bot speaking. No phoneme alignment is implied.',
      timing:'20 Hz preview target; randomized 3–6 second idle blinks, ~150 ms blink, ~5 second whisker twitch.',
      rendering:'Draw base once, restore underlying pixels in changed feature regions, then composite selected transparent patches. Store artwork in PSRAM or flash; budget LCD staging separately.',
      status:'Asset pack and browser simulation; LCD driver integration, memory placement, and on-device timing remain unverified.'
    }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = data;
  else root.HalloweenSprites = data;
})(typeof globalThis !== 'undefined' ? globalThis : this);
