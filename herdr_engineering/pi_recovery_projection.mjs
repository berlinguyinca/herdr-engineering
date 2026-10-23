// Read-only canonical projection. Never open(), append(), migrate or persist a session.
import { createHash } from 'node:crypto';
import { lstatSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const fail = code => { const error = new Error(code); error.projectionCode = code; throw error; };
const id = value => typeof value === 'string' && /^[A-Za-z0-9_./:-]{1,256}$/.test(value);
try {
  const input = JSON.parse(process.argv[1]);
  const readOwned = path => {
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.uid !== process.getuid() || stat.size > 64 * 1024 * 1024) fail('file_custody');
    return readFileSync(path);
  };
  const checkSdk = () => {
    for (const file of input.sdk.files) if (hash(readOwned(file.path)) !== file.sha256) fail('sdk_changed');
  };
  checkSdk();
  const bytes = readOwned(input.path);
  if (hash(bytes) !== input.digest) fail('transcript_changed');
  const lines = bytes.toString('utf8').split('\n').filter(line => line.trim());
  const valid = [], invalid = [];
  for (let index = 0; index < lines.length; index++) {
    try { valid.push(JSON.parse(lines[index])); }
    catch {
      const hint = /^\s*\{\s*"type"\s*:\s*"[^"\r\n]+"\s*,\s*"id"\s*:\s*"([A-Za-z0-9_.:-]{1,256})"/.exec(lines[index]);
      if (!hint || index === 0 || index === lines.length - 1) fail('malformed_line_unproven');
      invalid.push(hint[1]);
    }
  }
  const header = valid[0];
  if (!header || header.type !== 'session' || header.id !== input.sessionId || header.version !== 3 || typeof header.cwd !== 'string') fail('session_header');
  const indexed = new Map(), order = new Map();
  for (let index = 1; index < valid.length; index++) {
    const entry = valid[index];
    if (!entry || typeof entry !== 'object' || entry.type === 'session' || !id(entry.id) || indexed.has(entry.id) || !(entry.parentId === null || id(entry.parentId))) fail('entry_identity');
    indexed.set(entry.id, entry); order.set(entry.id, index);
  }
  if (!indexed.size) fail('empty_projection');
  const chain = [], seen = new Set();
  let cursor = valid.at(-1), missing = null;
  while (cursor) {
    if (seen.has(cursor.id)) fail('parent_cycle');
    seen.add(cursor.id); chain.push(cursor);
    if (cursor.parentId === null) break;
    if (!indexed.has(cursor.parentId)) { missing = cursor.parentId; break; }
    if (order.get(cursor.parentId) >= order.get(cursor.id)) fail('parent_order');
    cursor = indexed.get(cursor.parentId);
  }
  chain.reverse();
  if (invalid.some(value => seen.has(value) || value === missing || indexed.has(value))) fail('malformed_active_identity');
  const { SessionManager, parseSessionEntries, CURRENT_SESSION_VERSION } = await import(pathToFileURL(input.sdk.module).href);
  if (CURRENT_SESSION_VERSION !== 3 || typeof SessionManager.inMemory !== 'function') fail('sdk_api');
  const parsed = parseSessionEntries(bytes.toString('utf8'));
  if (parsed.length !== valid.length || parsed[0].id !== header.id) fail('parser_disagreement');
  const manager = SessionManager.inMemory(header.cwd, undefined, parsed);
  const branch = manager.getBranch();
  if (JSON.stringify(branch.map(entry => entry.id)) !== JSON.stringify(chain.map(entry => entry.id))) fail('branch_disagreement');
  const projection = manager.buildSessionProjection();
  let checkpointUsed = false;
  if (missing) {
    const checkpoint = chain.filter(entry => entry.type === 'compaction').at(-1);
    const kept = checkpoint ? chain.findIndex(entry => entry.id === checkpoint.firstKeptEntryId) : -1;
    const checkpointIndex = checkpoint ? chain.indexOf(checkpoint) : -1;
    if (!checkpoint || typeof checkpoint.summary !== 'string' || !checkpoint.summary.trim() || !Number.isFinite(checkpoint.tokensBefore) || checkpoint.tokensBefore <= 0 || kept < 0 || kept > checkpointIndex || projection.entries[0]?.sourceEntry.id !== checkpoint.id) fail('missing_parent');
    if (projection.entries.some(entry => entry.sourceEntry.id !== checkpoint.id && chain.findIndex(source => source.id === entry.sourceEntry.id) < kept)) fail('checkpoint_scope');
    checkpointUsed = true;
  }
  const pending = new Set(), calls = new Set();
  let lastMessage = null, hasImages = false, count = 0;
  const outage = new RegExp(input.outagePattern, 'i');
  for (const entry of projection.entries) {
    if (!seen.has(entry.sourceEntry.id) || !Array.isArray(entry.messages)) fail('projection_provenance');
    for (const message of entry.messages) {
      count++;
      if (message.role === 'toolResult' && id(message.toolCallId)) pending.delete(message.toolCallId);
      let toolCalls = 0;
      for (const part of Array.isArray(message.content) ? message.content : []) {
        if (part?.type === 'image') hasImages = true;
        if (part?.type === 'toolCall') {
          if (!id(part.id) || calls.has(part.id)) fail('duplicate_tool_identity');
          calls.add(part.id); pending.add(part.id); toolCalls++;
        }
      }
      lastMessage = {sourceId: entry.sourceEntry.id, role: ['assistant','user','toolResult','system','custom','branchSummary','compactionSummary'].includes(message.role) ? message.role : 'other', stopReason: ['error','stop','toolUse','aborted','length'].includes(message.stopReason) ? message.stopReason : null, outage: message.role === 'assistant' && message.stopReason === 'error' && outage.test(String(message.errorMessage ?? '')), toolCalls};
    }
  }
  checkSdk();
  if (hash(readOwned(input.path)) !== input.digest) fail('transcript_changed');
  const model = projection.model;
  console.log(JSON.stringify({verified: true, digest: input.digest, sdkVersion: input.sdk.version, branchLength: branch.length, projectedMessageCount: count, invalidHistoricalLines: invalid.length, checkpointUsed, pendingToolCalls: pending.size, hasImages, lastMessage, model: model && id(model.provider) && id(model.modelId) ? model : null}));
} catch (error) {
  console.log(JSON.stringify({verified: false, errorCode: error.projectionCode ?? 'sdk_projection_unavailable'}));
}
