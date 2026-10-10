import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, basename } from 'node:path';
import { spawnSync } from 'node:child_process';
import { ensureInstalled, downloadVerified } from './launcher.mjs';
const check=JSON.parse(await readFile(process.argv[2], 'utf8'));
const archive=check.archive;
const hash=createHash('sha256'); for await (const b of createReadStream(archive)) hash.update(b);
const sha256=hash.digest('hex');
const manifest={version:check.version,targets:{[`${process.platform}-${process.arch}`]:{sha256,url:`https://github.com/polaris-smart/agent-mailbox/releases/download/v${check.version}/${basename(archive)}`}}};
const root=await mkdtemp(join(tmpdir(),'mailbox npm native '));
try {
 const binary=await ensureInstalled(manifest,{root,download:(url,destination,sum)=>downloadVerified(url,destination,sum,async()=>new Response(await readFile(archive)))});
 const result=spawnSync(binary,['--version'],{encoding:'utf8',timeout:30000});
 if(result.status!==0 || !result.stdout.includes(check.version)) throw new Error(`Native version failed: ${result.status} ${result.stderr}`);
 if(await ensureInstalled(manifest,{root})!==binary) throw Error('Cache reuse failed');
 console.log(JSON.stringify({version:check.version,platform:process.platform,arch:process.arch,checksum:sha256,nativeExtraction:true,versionPassthrough:true,cacheReuse:true}));
} finally {await rm(root,{recursive:true,force:true});}
