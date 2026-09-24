import assert from 'node:assert/strict';
import test from 'node:test';
import {parseDouyinTask,douyinActive,douyinRequest} from '../app/douyin-api.ts';
const task={id:'a'.repeat(32),url:'https://douyin.com/video/1',mode:'download',status:'queued',progress:0};
test('download receipts reject invalid identities and states',()=>{
 for(const value of [null,{}, {...task,id:'../x'}, {...task,status:'oops'}]) assert.throws(()=>parseDouyinTask(value));
 assert.equal(douyinActive(parseDouyinTask(task)),true);
 assert.equal(parseDouyinTask({...task,progress:Infinity}).progress,0);
 assert.equal(parseDouyinTask({...task,progress:150}).progress,100);
 assert.equal(douyinActive(parseDouyinTask({...task,status:'completed'})),false);
});
test('API uses same origin and surfaces actionable errors', async()=>{
 const original=globalThis.fetch;
 try {
 globalThis.fetch=async(url,options)=>{assert.equal(url,'/api/douyin/tasks');assert.equal(options.method,'POST');return {ok:false,json:async()=>({error:{message:'fresh session required'}})};};
 await assert.rejects(()=>douyinRequest('tasks',{text:task.url}),/fresh session/);
 }finally{globalThis.fetch=original;}
});
