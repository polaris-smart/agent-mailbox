import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createHash } from 'node:crypto';
import { selectTarget, ensureInstalled, downloadVerified, executableRelative } from './launcher.mjs';
const sum = createHash('sha256').update('fixture').digest('hex');
const manifest = { version: '0.8.2', targets: Object.fromEntries(['darwin-arm64','darwin-x64','win32-x64','linux-x64'].map(key=>[key,{sha256:sum,url:`https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.2/Agent-Mailbox-0.8.2-${key}.zip`}])) };
async function temporary(fn) {const dir=await mkdtemp(join(tmpdir(),'mailbox npm test '));try{await fn(dir);}finally{await rm(dir,{recursive:true,force:true});}}
test('exact OS/CPU and release mapping; unsupported target and URL fail closed',()=>{
 for(const [p,a] of [['darwin','arm64'],['darwin','x64'],['win32','x64'],['linux','x64']]) assert.equal(selectTarget(manifest,p,a).key,`${p}-${a}`);
 assert.throws(()=>selectTarget(manifest,'linux','arm64'));
 const bad=structuredClone(manifest);bad.targets['darwin-arm64'].url='https://other.invalid/a.zip';assert.throws(()=>selectTarget(bad,'darwin','arm64'));
});
test('download hash checked; HTTP failure and tampered bytes rejected',()=>temporary(async dir=>{
 await downloadVerified('https://fixture.invalid',join(dir,'good'),sum,async()=>new Response('fixture'));
 assert.equal(await readFile(join(dir,'good'),'utf8'),'fixture');
 await assert.rejects(downloadVerified('https://fixture.invalid',join(dir,'bad'),sum,async()=>new Response('tampered')),/checksum mismatch/);
 await assert.rejects(downloadVerified('https://fixture.invalid',join(dir,'missing'),sum,async()=>new Response('',{status:404})),/HTTP 404/);
}));
test('all target paths with spaces; cache reuse never redownloads',async()=>{
 for(const platform of ['darwin','win32','linux']) await temporary(async root=>{
  let downloads=0;
  const opts={root,platform,arch:'x64',log:()=>{},download:async()=>{downloads++},extract:async(a,d)=>{const file=join(d,executableRelative(platform));await mkdir(dirname(file),{recursive:true});await writeFile(file,'fixture');}};
  const bin=await ensureInstalled(manifest,opts);assert.equal(await readFile(bin,'utf8'),'fixture');
  assert.equal(await ensureInstalled(manifest,opts),bin);assert.equal(downloads,1);
 });
});
test('download failure never extracts and cleans temporary state',()=>temporary(async root=>{
 let extracts=0;
 await assert.rejects(ensureInstalled(manifest,{root,platform:'darwin',arch:'arm64',log:()=>{},download:async()=>{throw Error('checksum mismatch')},extract:async()=>{extracts++}}));
 assert.equal(extracts,0);assert.deepEqual(await readdir(root),[]);
}));
test('extraction failure and missing binary cannot make a complete installation',()=>temporary(async root=>{
 for(const ex of [async()=>{throw Error('bad archive')},async()=>{}]){
  await assert.rejects(ensureInstalled(manifest,{root,platform:'linux',arch:'x64',log:()=>{},download:async()=>{},extract:ex}));
  assert.deepEqual(await readdir(root),[]);
 }
}));
test('concurrent preparations converge on one verified executable',()=>temporary(async root=>{
 const opts={root,platform:'darwin',arch:'arm64',log:()=>{},download:async()=>{},extract:async(a,d)=>{const f=join(d,executableRelative('darwin'));await mkdir(dirname(f),{recursive:true});await writeFile(f,'fixture')}};
 const results=await Promise.all([ensureInstalled(manifest,opts),ensureInstalled(manifest,opts)]);
 assert.equal(results[0],results[1]);assert.equal((await readdir(root)).length,1);
}));
