#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { ensureInstalled, launch } from './launcher.mjs';
try {
  const manifest = JSON.parse(await readFile(new URL('./releases.json', import.meta.url), 'utf8'));
  const binary = await ensureInstalled(manifest);
  process.exitCode = await launch(binary, process.argv.slice(2));
} catch (error) {
  console.error(`agent-mailbox: ${error.message}`);
  process.exitCode = 1;
}
