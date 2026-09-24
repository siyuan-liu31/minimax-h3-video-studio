import assert from 'node:assert/strict';
import test from 'node:test';
import {explainDouyinFailure} from '../app/douyin-feedback.ts';

test('rejected video feedback distinguishes server and local recovery',()=>{
 const local=explainDouyinFailure({code:'cookie_refresh_required',source:'local',stage:'import'});
 assert.match(local.title,/拒绝/);
 assert.match(local.detail,/不能据此断定 Cookie 已过期/);
 assert.match(local.nextStep,/停止连续重试/);
 assert.match(local.diagnostic,/本机 · 导入视频 · cookie_refresh_required/);
 const server=explainDouyinFailure({code:'cookie_refresh_required',source:'server',stage:'parsing'});
 assert.match(server.nextStep,/本机导入/);
});

test('feedback distinguishes platform limit, bridge limit, and upload failure',()=>{
 assert.match(explainDouyinFailure({code:'rate_limited',source:'local'}).title,/抖音/);
 assert.match(explainDouyinFailure({code:'bridge_rate_limited',source:'local'}).title,/本机/);
 const upload=explainDouyinFailure({code:'upload_failed',message:'storage full',stage:'upload',source:'local'});
 assert.match(upload.title,/上传资产库失败/);
 assert.equal(upload.detail,'storage full');
 assert.match(upload.diagnostic,/上传资产/);
});
