import test from 'node:test';
import assert from 'node:assert/strict';
import { emptyLyricsDraft, applyTranscription, readLyricsDraft, saveLyricsDraft, lyricLines } from '../app/lyrics-draft.ts';
const source = 'a'.repeat(32);
const task = { id: 'b'.repeat(32), operation: 'transcribe', status: 'completed', sourceAssetId: source, detectedLyrics: '原词\n第二行' };
test('ASR populates untouched drafts but never overwrites user edits or another song', () => {
 const initial = emptyLyricsDraft(source);
 assert.equal(applyTranscription(initial, task).lyrics, task.detectedLyrics);
 const edited = {...initial, lyrics:'我的新词', edited:true, original:'校正词', originalEdited:true};
 const applied = applyTranscription(edited, task);
 assert.equal(applied.lyrics, '我的新词'); assert.equal(applied.original, '校正词');
 assert.equal(applyTranscription(applied, task), applied);
 assert.equal(applyTranscription(emptyLyricsDraft('c'.repeat(32)),task).lyrics,'');
 assert.equal(applyTranscription(initial,{...task,status:'failed'}),initial);
});
test('draft survives reload and source switching with corruption fallback', () => {
 const first = {...emptyLyricsDraft(source),lyrics:'我的草稿',edited:true};
 const second = {...emptyLyricsDraft('c'.repeat(32)), lyrics:'另一首'};
 const raw=saveLyricsDraft(saveLyricsDraft(null,first),second);
 assert.deepEqual(readLyricsDraft(raw,source),first); assert.deepEqual(readLyricsDraft(raw),second);
 assert.equal(readLyricsDraft('broken',source).sourceId,source);
 assert.equal(lyricLines('一二，三','一二')[0].before,3);
});
