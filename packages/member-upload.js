// Chunked, resumable upload of one file for the member web UI.
//
// Pass 1 hashes the file in order (the server derives the upload id from the whole-file sha256, so it is needed first).
// Pass 2 asks the server which chunks it already holds and sends the rest as runs of consecutive chunks, a few runs at a
// time. Reload, pause and retry all come down to calling uploadFile again with the same file.
import { createSha256 } from './sha256-stream.js';

export const CHUNK_BYTES = 524288;
export const MAX_CHUNKS_PER_RUN = 32;          // the server and the edge both cap a run body at 32 chunks (16 MiB)
export const MAX_RUNS_IN_FLIGHT = 3;
const HASH_BLOCK = 4 * 1024 * 1024;
const MAX_ATTEMPTS = 6;
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 30000;

export function abortError() {
  return Object.assign(new Error('upload aborted'), { name: 'AbortError' });
}

function checkAbort(signal) {
  if (signal?.aborted) throw abortError();
}

const hex = (buffer) => Array.from(new Uint8Array(buffer), (b) => b.toString(16).padStart(2, '0')).join('');

function defaultSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); reject(abortError()); };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/** The set of chunk indexes the server holds, from its inclusive missing ranges. */
export function missingToPresent(missing, total) {
  const absent = new Set();
  for (const [from, to] of missing || []) for (let i = from; i <= to && i < total; i += 1) absent.add(i);
  const present = new Set();
  for (let i = 0; i < total; i += 1) if (!absent.has(i)) present.add(i);
  return present;
}

async function hashFile(file, signal, onProgress) {
  const hash = createSha256();
  for (let offset = 0; offset < file.size; offset += HASH_BLOCK) {
    checkAbort(signal);
    hash.update(new Uint8Array(await file.slice(offset, Math.min(file.size, offset + HASH_BLOCK)).arrayBuffer()));
    onProgress({ phase: 'hashing', done: Math.min(file.size, offset + HASH_BLOCK), total: file.size });
  }
  return hash.digestHex();
}

const retryable = (error) => error?.name !== 'AbortError' && (error?.status === undefined || error.status === 429 || error.status >= 500);

async function withRetry(operation, { signal, sleep }) {
  for (let attempt = 0; ; attempt += 1) {
    checkAbort(signal);
    try {
      return await operation();
    } catch (error) {
      if (!retryable(error) || attempt + 1 >= MAX_ATTEMPTS) throw error;
      const backoff = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
      const wait = Math.max(backoff, Number.isFinite(error?.retryAfter) ? error.retryAfter * 1000 : 0);
      await sleep(wait, signal);
    }
  }
}

/**
 * @param {File} file
 * @param {{client, artifactId: string, signal?: AbortSignal, onProgress?: Function, chunksPerRequest?: number,
 *          knownSha256?: string, sleep?: Function}} options
 *   client.status(artifactId, {name, size_bytes, file_sha256}, {signal}) -> {upload_id, total, missing_ranges}
 *   client.putRun(artifactId, uploadId, firstIndex, bytes, {name, size_bytes, file_sha256, run_sha256, signal})
 * @returns {Promise<{upload_id: string, file_sha256: string}>}
 */
export async function uploadFile(file, { client, artifactId, signal, onProgress = () => {}, chunksPerRequest = MAX_CHUNKS_PER_RUN, knownSha256, sleep = defaultSleep } = {}) {
  if (!file || file.size < 1) throw new Error('empty file: nothing to upload');
  const perRun = Math.max(1, Math.min(MAX_CHUNKS_PER_RUN, Math.floor(chunksPerRequest) || MAX_CHUNKS_PER_RUN));
  const size = file.size;
  checkAbort(signal);
  const fileSha = knownSha256 || await hashFile(file, signal, onProgress);
  const meta = { name: file.name, size_bytes: size, file_sha256: fileSha };
  const status = await withRetry(() => client.status(artifactId, meta, { signal }), { signal, sleep });
  const total = Math.ceil(size / CHUNK_BYTES);
  const present = missingToPresent(status.missing_ranges, total);
  const runs = [];
  for (let first = 0; first < total; first += perRun) {
    const indexes = Array.from({ length: Math.min(perRun, total - first) }, (_, i) => first + i);
    runs.push({ first, bytes: Math.min(size, (first + indexes.length) * CHUNK_BYTES) - first * CHUNK_BYTES, skip: indexes.every((i) => present.has(i)) });
  }
  let done = runs.filter((r) => r.skip).reduce((sum, r) => sum + r.bytes, 0);
  onProgress({ phase: 'uploading', done, total: size });
  const queue = runs.filter((r) => !r.skip);
  let failure = null;
  async function worker() {
    while (queue.length && failure === null) {
      const run = queue.shift();
      try {
        checkAbort(signal);
        const body = new Uint8Array(await file.slice(run.first * CHUNK_BYTES, run.first * CHUNK_BYTES + run.bytes).arrayBuffer());
        const runSha = hex(await crypto.subtle.digest('SHA-256', body));
        await withRetry(() => client.putRun(artifactId, status.upload_id, run.first, body, { ...meta, run_sha256: runSha, signal }), { signal, sleep });
        done += run.bytes;
        onProgress({ phase: 'uploading', done, total: size });
      } catch (error) {
        failure ??= error;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(MAX_RUNS_IN_FLIGHT, queue.length) }, worker));
  if (failure !== null) throw failure;
  checkAbort(signal);
  onProgress({ phase: 'done', done: size, total: size });
  return { upload_id: status.upload_id, file_sha256: fileSha };
}
