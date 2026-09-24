import assert from 'node:assert/strict';
import test from 'node:test';
import {connectDouyinBridge, DouyinBridgeError, importDouyinViaBridge, inspectDouyinViaBridge} from '../app/douyin-local-bridge.ts';

const token='a'.repeat(48);
const taskId='b'.repeat(32);
const downloadToken='c'.repeat(48);
const assetId='d'.repeat(32);

test('Studio discovers the local bridge and imports its video through the asset API', async()=>{
 const original=globalThis.fetch;
 const calls=[];
 try{
  globalThis.fetch=async(url,init={})=>{
   calls.push([url,init]);
   if(url==='http://127.0.0.1:8765/health') return Response.json({status:'ok',api_version:'h3ctl.douyin/v1',bridge_token:token});
   if(url==='http://127.0.0.1:8765/api/inspect') return Response.json({metadata:{id:'video123',title:'Sample'}});
   if(url==='http://127.0.0.1:8765/api/parse') return Response.json({task:{id:taskId,status:'completed',download_url:`/api/download/${downloadToken}`,download:{size:5},metadata:{id:'video123',ext:'mp4'}}},{status:202});
   if(url===`http://127.0.0.1:8765/api/download/${downloadToken}`) return new Response(new Blob(['video'],{type:'video/mp4'}));
   if(url==='/api/assets') return Response.json({asset:{id:assetId,kind:'video',filename:'video123.mp4',media:{duration:2}}});
   throw Error(`unexpected ${url}`);
  };
  const discovered=await connectDouyinBridge();
  assert.equal(discovered,token);
  assert.equal((await inspectDouyinViaBridge('https://v.douyin.com/abc/',discovered)).title,'Sample');
  const asset=await importDouyinViaBridge('https://v.douyin.com/abc/',discovered,()=>{});
  assert.equal(asset.id,assetId);
  assert.equal(calls[1][1].headers.get('X-H3-Douyin-Token'),token);
  assert.equal(calls[3][1].headers.get('X-H3-Douyin-Token'),token);
  assert.equal(calls[4][1].body.get('file').name,'video123.mp4');
 }finally{globalThis.fetch=original;}
});

test('Studio refuses an invalid bridge download path', async()=>{
 const original=globalThis.fetch;
 try{
  globalThis.fetch=async(url)=>{
   if(url==='http://127.0.0.1:8765/api/parse') return Response.json({task:{id:taskId,status:'completed',download_url:'http://example.com/video.mp4'}},{status:202});
   throw Error(`unexpected ${url}`);
  };
  await assert.rejects(()=>importDouyinViaBridge('https://v.douyin.com/abc/',token,()=>{}),/下载地址/);
 }finally{globalThis.fetch=original;}
});

test('Studio explains a rejected Douyin session without retrying automatically', async()=>{
 const original=globalThis.fetch;
 let calls=0;
 try{
  globalThis.fetch=async(url)=>{
   calls++;
   if(url==='http://127.0.0.1:8765/api/parse') return Response.json({task:{id:taskId,status:'failed',error:{code:'cookie_refresh_required',message:'fresh session required'}}},{status:202});
   throw Error(`unexpected ${url}`);
  };
  await assert.rejects(()=>importDouyinViaBridge('https://v.douyin.com/abc/',token,()=>{}),(error)=>{
   assert.ok(error instanceof DouyinBridgeError);
   assert.equal(error.code,'cookie_refresh_required');
   assert.equal(error.stage,'import');
   return true;
  });
  assert.equal(calls,1);
 }finally{globalThis.fetch=original;}
});

test('Studio preserves the upload failure stage for visible feedback', async()=>{
 const original=globalThis.fetch;
 try{
  globalThis.fetch=async(url)=>{
   if(url==='http://127.0.0.1:8765/api/parse') return Response.json({task:{id:taskId,status:'completed',download_url:`/api/download/${downloadToken}`,download:{size:5},metadata:{id:'video123',ext:'mp4'}}},{status:202});
   if(url===`http://127.0.0.1:8765/api/download/${downloadToken}`) return new Response(new Blob(['video'],{type:'video/mp4'}));
   if(url==='/api/assets') return Response.json({error:{message:'storage full'}},{status:507});
   throw Error(`unexpected ${url}`);
  };
  await assert.rejects(()=>importDouyinViaBridge('https://v.douyin.com/abc/',token,()=>{}),(error)=>{
   assert.ok(error instanceof DouyinBridgeError);
   assert.equal(error.code,'upload_failed');
   assert.equal(error.stage,'upload');
   return true;
  });
 }finally{globalThis.fetch=original;}
});
