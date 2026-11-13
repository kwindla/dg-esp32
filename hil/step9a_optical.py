#!/usr/bin/env python3
"""New-pose calibration followed by physical disc, patch geometry, and A8 checks."""
import argparse,hashlib,json,subprocess,threading,time,sys
from collections import deque
from pathlib import Path
import numpy as np
from scipy import ndimage
from esp_serial import EspSerial,ROOT
from step9a_assets import asset_server
from step9a_support import read,request,terminal,host_address,FIXTURES
from display_test import save_png
WIDTH,HEIGHT=1280,720
sys.path.insert(0,str(ROOT/'tools'))
from dgi import encode

class Camera:
 def __init__(self,out):
  self.out=out;self.frames=deque(maxlen=16);self.roi=None;self.rows=[];self.done=threading.Event();self.log=(out/'camera-kernel-pts.log').open('w')
  self.process=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','info','-copyts','-f','v4l2','-input_format','mjpeg','-video_size','1280x720','-framerate','30','-i','/dev/video0','-vf','format=rgb24,showinfo','-f','rawvideo','pipe:1'],stdout=subprocess.PIPE,stderr=self.log)
  self.thread=threading.Thread(target=self.reader,daemon=True);self.thread.start()
 def reader(self):
  while not self.done.is_set():
   raw=self.process.stdout.read(WIDTH*HEIGHT*3)
   if len(raw)!=WIDTH*HEIGHT*3:break
   stamp=time.monotonic();n=len(self.rows);self.frames.append((n,stamp,np.frombuffer(raw,dtype=np.uint8).reshape(HEIGHT,WIDTH,3).copy()));self.rows.append({'frame':n,'host_received_s':stamp})
 def reference(self):
  deadline=time.monotonic()+5
  while len(self.rows)<30 and time.monotonic()<deadline:time.sleep(.05)
  if len(self.rows)<30:raise RuntimeError('Webcam did not provide a fresh reference frame')
  n,stamp,rgb=list(self.frames)[-1];path=self.out/'fresh-camera-reference.png';save_png(path,rgb)
  return {'frame_index':n,'host_received_s':stamp,'source_kernel_pts':'same frame index in camera-kernel-pts.log','png_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'basis':'one newly captured full frame, no spatial crop or frame averaging'}
 def shot(self,name,*,full=False):
  start=time.monotonic();deadline=start+5
  while time.monotonic()<deadline:
   selected=[rgb for n,stamp,rgb in list(self.frames) if stamp>start+1.0]
   if len(selected)>=8:
    rgb=np.median(np.stack(selected[-8:]),axis=0).astype(np.uint8)
    if self.roi is not None and not full:
     x0,y0,x1,y1=self.roi;rgb=rgb[y0:y1,x0:x1]
    save_png(self.out/(name+'.png'),rgb);return rgb.astype(float)
   time.sleep(.05)
  raise RuntimeError('Webcam did not provide eight fresh frames')
 def close(self):
  self.done.set();self.process.terminate()
  try:self.process.wait(timeout=5)
  except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
  self.thread.join(timeout=3);self.log.close();(self.out/'camera-frames.json').write_text(json.dumps(self.rows)+'\n')

def fixture(name,w,h,pixels):
 data=encode(w,h,pixels);digest=hashlib.sha256(data).hexdigest();(FIXTURES/(digest+'.dgi')).write_bytes(data);return {'name':name,'hash':digest,'bytes':len(data),'width':w,'height':h}

def homography(source,destination):
 a=[];b=[]
 for (x,y),(u,v) in zip(source,destination):
  a.extend([[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]]);b.extend([u,v])
 return np.append(np.linalg.solve(np.asarray(a),np.asarray(b)),1).reshape(3,3)

def main():
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
 result={'passed':False,'basis':'new user-moved camera pose; full-frame reference and differential lit-disc detection derive the crop; four new marker centroids fit homography with independent center check. Kernel frame PTS retained. Historical fixed ROIs are never used. No tear-free or lip-skew claim.','checks':{}}
 (a.out/'plan.json').write_text(json.dumps({'markers':[[128,128],[284,128],[128,284],[284,284]],'patch':[92,176,224,56],'odd_y_patch':[152,201,96,17],'alpha':[0,64,128,192,255],'required':'lit disc, green patch inside calibrated rectangle, unchanged exterior; odd Y alignment where row bytes are multiple16; increasing A8 white shades','pose':'user moved webcam; fresh full-frame capture and calibration required','old_fixed_coordinates_used':False,'camera_size':[WIDTH,HEIGHT],'retakes':False},indent=2)+'\n')
 marker=fixture('red-marker',24,24,[(255,0,0,255)]*576);green=fixture('cat-union-green',224,56,[(0,255,0,255)]*(224*56));odd=fixture('odd-y-green',96,17,[(0,255,0,255)]*(96*17));alpha=fixture('A8-shades',100,40,[(255,255,255,[0,64,128,192,255][x//20]) for y in range(40) for x in range(100)])
 camera=None
 try:
  with asset_server(),EspSerial('/dev/ttyACM0',log_path=a.out/'serial.log',display_checks=True) as board:
   at=board.cursor;board.reset();board.wait_for('READY transport-console',60,since=at);board.command('quiet 1');board.command('disp init',30);board.command('disp origin http://'+host_address()+':7874');from step9a_support import BUDGET_PATH;budget=json.loads(BUDGET_PATH.read_text());board.command('disp arena {budget} {F} {W} {largest}'.format(**budget))
   for index,image in enumerate((marker,green,odd,alpha)):
    rid='opt-load-'+str(index);request(board,'load',{'hash':image['hash'],'bytes':image['bytes'],'url':'http://'+host_address()+':7874/'+image['hash']+'.dgi'},rid);assert terminal(board,rid)['error']=='none'
   number=0
   def scene(color=0,image=None,x=0,y=0):
    nonlocal number
    number+=1;ops=[{'op':'fill','color':color}]+[{'op':'hide','layer':i} for i in range(4)]
    if image:ops.append({'op':'show','layer':0,'hash':image['hash'],'x':x,'y':y})
    rid='opt-scene-'+str(number);request(board,'batch',{'ops':ops},rid);assert terminal(board,rid)['error']=='none'
   camera=Camera(a.out);fresh=camera.reference()
   scene();black_full=camera.shot('black-calibration-full',full=True);scene(color=65535);white_full=camera.shot('lit-disc-reference-full',full=True)
   difference=np.mean(white_full-black_full,axis=2);labels,count=ndimage.label(difference>70)
   sizes=np.bincount(labels.ravel());sizes[0]=0;component=int(sizes.argmax());disc=labels==component;ys,xs=np.where(disc)
   if len(xs)<500 or xs.min()==0 or ys.min()==0 or xs.max()==WIDTH-1 or ys.max()==HEIGHT-1:raise RuntimeError('New-pose calibration inconclusive: complete lit disc not detected')
   pad=max(20,int(max(xs.max()-xs.min(),ys.max()-ys.min())*.15));roi=[max(0,int(xs.min())-pad),max(0,int(ys.min())-pad),min(WIDTH,int(xs.max())+pad+1),min(HEIGHT,int(ys.max())+pad+1)];camera.roi=roi;x0,y0,x1,y1=roi;disc_crop=disc[y0:y1,x0:x1];black=black_full[y0:y1,x0:x1];disc_area=int(disc_crop.sum());locations=[];logical=[[128,128],[284,128],[128,284],[284,284]]
   result['pose_calibration']={'source_size':[WIDTH,HEIGHT],'old_fixed_coordinates_used':False,'lit_disc_pixels':disc_area,'lit_disc_bbox_full':[int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)],'lit_disc_centroid_full':[float(xs.mean()),float(ys.mean())],'derived_crop_full':roi,'detection':'largest connected component of full white-minus-black meanRGB >70','fresh_reference':'fresh-camera-reference.png','fresh_frame':fresh,'lit_reference':'lit-disc-reference-full.png'}
   save_png(a.out/'new-pose-disc-reference.png',white_full[y0:y1,x0:x1].astype(np.uint8))
   (a.out/'new-pose-calibration.json').write_text(json.dumps(result['pose_calibration'],indent=2)+'\n')
   for index,(cx,cy) in enumerate(logical):
    scene(image=marker,x=cx-12,y=cy-12);rgb=camera.shot('marker-'+str(index));mask=(rgb[:,:,0]>80)&(rgb[:,:,0]>2*np.maximum(rgb[:,:,1],rgb[:,:,2]))&((rgb-black)[:,:,0]>50)&disc_crop;marker_labels,n=ndimage.label(mask);marker_sizes=np.bincount(marker_labels.ravel());marker_sizes[0]=0;ys,xs=np.where(marker_labels==marker_sizes.argmax());assert 4<=len(xs)<=disc_area*.03,('marker-mask',index,len(xs));locations.append([float(xs.mean()),float(ys.mean())])
   H=homography(logical,locations);result['calibration']={'logical':logical,'camera_crop_centroids':locations,'homography':H.tolist(),'derived_crop_full':roi};crop_height,crop_width=black.shape[:2];yy,xx=np.indices((crop_height,crop_width));points=np.stack((xx.ravel(),yy.ravel(),np.ones(crop_width*crop_height)));uv=np.linalg.inv(H)@points;lx=(uv[0]/uv[2]).reshape(crop_height,crop_width);ly=(uv[1]/uv[2]).reshape(crop_height,crop_width);radius=np.hypot(lx-206,ly-206)
   scene(image=marker,x=194,y=194);rgb=camera.shot('center-geometry-check');mask=(rgb[:,:,0]>80)&(rgb[:,:,0]>2*np.maximum(rgb[:,:,1],rgb[:,:,2]))&((rgb-black)[:,:,0]>50)&disc_crop;marker_labels,n=ndimage.label(mask);marker_sizes=np.bincount(marker_labels.ravel());marker_sizes[0]=0;ys,xs=np.where(marker_labels==marker_sizes.argmax());assert 4<=len(xs)<=disc_area*.03
   predicted=H@np.array([206,206,1]);predicted=predicted[:2]/predicted[2];observed=np.array([xs.mean(),ys.mean()]);error=float(np.linalg.norm(predicted-observed));tolerance=max(2.0,.015*max(x1-x0,y1-y0));assert error<=tolerance,('independent-center-calibration',error,tolerance)
   def polygon(rect):
    x,y,w,h=rect;corners=np.array([[x,y,1],[x+w,y,1],[x+w,y+h,1],[x,y+h,1]]).T;points=H@corners;points=(points[:2]/points[2]).T;return (points+np.array([x0,y0])).tolist()
   result['pose_calibration'].update(result['calibration']);result['pose_calibration']['center_check']={'predicted_crop':predicted.tolist(),'observed_crop':observed.tolist(),'error_pixels':error,'tolerance_pixels':tolerance};result['pose_calibration']['roi_polygons_full']={name:polygon(rect) for name,rect in {'cat_union':[92,176,224,56],'odd_y':[152,201,96,17],'A8':[150,150,100,40]}.items()};result['pose_calibration']['A8_band_polygons_full']=[polygon([150+i*20,150,20,40]) for i in range(5)];(a.out/'new-pose-calibration.json').write_text(json.dumps(result['pose_calibration'],indent=2)+'\n')
   scene(color=65535);white=camera.shot('white-disc');inside=radius<185;outside=radius>220;change=np.mean(white-black,axis=2);lit=float(np.mean(change[inside]>70));spill=float(np.mean(change[outside]>70));result['checks']['lit_disc']={'inside_lit_fraction':lit,'outside_lit_fraction':spill,'passed':lit>.95 and spill<.05};assert result['checks']['lit_disc']['passed']
   def patch(name,image,x,y):
    scene();baseline=camera.shot(name+'-before');scene(image=image,x=x,y=y);rgb=camera.shot(name+'-after');w,h=image['width'],image['height'];inside=(lx>x+5)&(lx<x+w-5)&(ly>y+3)&(ly<y+h-3);exterior=(radius<185)&((lx<x-8)|(lx>x+w+8)|(ly<y-8)|(ly>y+h+8));green_pixels=(rgb[:,:,1]>60)&(rgb[:,:,1]>1.4*np.maximum(rgb[:,:,0],rgb[:,:,2]));good=float(np.mean(green_pixels[inside]));stable=float(np.mean(np.max(np.abs(rgb-baseline),axis=2)[exterior]<35));d={'logical_rect':[x,y,w,h],'inside_samples':int(inside.sum()),'exterior_samples':int(exterior.sum()),'green_fraction':good,'unchanged_exterior_fraction':stable,'passed':inside.sum()>8 and good>.9 and stable>.95};result['checks'][name]=d;assert d['passed'],d
   patch('cat_union',green,92,176);patch('odd_y_row_multiple16',odd,152,201)
   scene(image=alpha,x=150,y=150);rgb=camera.shot('A8-white-shades');levels=[]
   for index in range(5):
    mask=(lx>150+index*20+4)&(lx<150+(index+1)*20-4)&(ly>157)&(ly<183);levels.append(float(np.mean(rgb[mask])))
   d={'alphas':[0,64,128,192,255],'camera_mean_rgb_levels':levels,'basis':'relative optical monotonicity; exact integer blending verified in independent host tests','passed':levels[0]<55 and levels[-1]>120 and all(b-a>8 for a,b in zip(levels,levels[1:]))};result['checks']['A8']=d;assert d['passed'],d
   result['status']=read(board,'status','DISPLAY');result['te']=read(board,'te','DISPLAY_TE');assert result['status']['dma']==[0,0] and not result['status']['failed'];scene();board.command('audio 0');board.command('disconnect',45);result['passed']=True
 except Exception as error:result['error']=f'{type(error).__name__}: {error}';raise
 finally:
  if camera:camera.close()
  (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result['checks']),flush=True)
if __name__=='__main__':main()
