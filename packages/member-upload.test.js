import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import { createMemberApi } from './member-api.js';
import { uploadFile, CHUNK_BYTES, MAX_CHUNKS_PER_RUN, MAX_RUNS_IN_FLIGHT, missingToPresent } from './member-upload.js';

const MIB = 1024 * 1024;
// The server's own derivation (python upload_id_for) for one fixed input; tests/control_plane/test_member_upload_vector.py keeps it honest.
const VECTOR = JSON.parse(fs.readFileSync(new URL('./member-upload-vector.json', import.meta.url), 'utf8'));
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const noSleep = { sleep: async () => {} };

function bytesOf(size) {
  const data = new Uint8Array(size);
  for (let i = 0; i < size; i += 1) data[i] = (i * 31 + (i >> 8)) & 255;
  return data;
}

/**
 * A real node:http server that behaves like the member API: the upload id derives from the whole-file sha256 and runs are
 * stored by chunk. The uploader talks to it through the real createMemberApi client, so headers and bodies cross a socket.
 * `failures` is a queue of {at, status, retryAfter}: the next put at chunk `at` answers that status instead of storing.
 */
class UploadServer {
  constructor() {
    this.stored = new Set(); this.events = []; this.puts = []; this.inFlight = 0; this.maxInFlight = 0;
    this.failures = []; this.cap = Infinity; this.statusCalls = 0; this.violations = [];
  }
  static async start() {
    const server = new UploadServer();
    server.http = http.createServer((req, res) => { server.handle(req, res).catch((e) => { server.violations.push(String(e)); res.writeHead(500).end(); }); });
    await new Promise((resolve) => server.http.listen(0, '127.0.0.1', resolve));
    server.base = `http://127.0.0.1:${server.http.address().port}`;
    open.push(server);
    return server;
  }
  close() { this.http.closeAllConnections(); return new Promise((resolve) => this.http.close(resolve)); }
  uploadId(artifactId, name, size, fileSha) { if (artifactId === VECTOR.artifact_id && name === VECTOR.name && size === VECTOR.size_bytes && fileSha === VECTOR.file_sha256) return VECTOR.upload_id; return `u_${sha(`${artifactId}|${name}|${size}|${fileSha}`).slice(0, 32)}`; }
  client() {
    return createMemberApi({ sessions: { get: () => 'session' }, refresh: async () => {}, base: this.base });
  }
  reply(res, status, body, headers = {}) {
    res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
    res.end(JSON.stringify(body));
  }
  async handle(req, res) {
    if (req.method === 'PUT') {                                          // in flight from the first byte of the request
      this.inFlight += 1;
      this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
      res.on('close', () => { this.inFlight -= 1; });
    }
    const chunks = [];
    for await (const part of req) chunks.push(part);
    const body = Buffer.concat(chunks);
    const url = new URL(req.url, 'http://x');
    const statusMatch = /^\/api\/member\/artifacts\/([^/]+)\/uploads\/status$/.exec(url.pathname);
    const runMatch = /^\/api\/member\/artifacts\/([^/]+)\/uploads\/([^/]+)\/runs\/(\d+)$/.exec(url.pathname);
    if (req.headers.authorization !== 'Member session') return this.reply(res, 401, { error: 'unauthorized' });
    if (req.method === 'POST' && statusMatch) return this.status(res, decodeURIComponent(statusMatch[1]), JSON.parse(body.toString('utf8')));
    if (req.method === 'PUT' && runMatch) return this.put(res, req, body, decodeURIComponent(runMatch[1]), decodeURIComponent(runMatch[2]), Number(runMatch[3]));
    return this.reply(res, 404, { error: 'not_found' });
  }
  status(res, artifactId, args) {
    this.events.push(['status', args.file_sha256]);
    const total = Math.ceil(args.size_bytes / CHUNK_BYTES);
    const ranges = [];
    let start = null;
    for (let i = 0; i <= total; i += 1) {
      const absent = i < total && !this.stored.has(i);
      if (absent && start === null) start = i;
      if (!absent && start !== null) { ranges.push([start, i - 1]); start = null; }
    }
    this.statusCalls += 1;
    return this.reply(res, 200, {
      upload_id: this.uploadId(artifactId, args.name, args.size_bytes, args.file_sha256), total,
      missing_ranges: ranges.slice(0, this.cap), complete: ranges.length === 0, truncated: ranges.length > this.cap,
    });
  }
  async put(res, req, bytes, artifactId, uploadId, first) {
    this.events.push(['put', first]);
    {
      await new Promise((resolve) => setTimeout(resolve, 2));          // let concurrent runs overlap
      const failure = this.failures[0];
      if (failure && failure.at === first) {
        this.failures.shift();
        return this.reply(res, failure.status, { error: `http_${failure.status}` }, failure.retryAfter ? { 'Retry-After': String(failure.retryAfter) } : {});
      }
      const name = decodeURIComponent(req.headers['x-name']);
      const size = Number(req.headers['x-size-bytes']);
      if (req.headers['x-run-sha256'] !== sha(bytes)) { this.violations.push('run sha256 header does not match the body'); return this.reply(res, 422, { error: 'run_sha' }); }
      if (uploadId !== this.uploadId(artifactId, name, size, req.headers['x-file-sha256'])) { this.violations.push('upload id mismatch'); return this.reply(res, 422, { error: 'upload_id' }); }
      this.puts.push({ first, length: bytes.length });
      for (let i = 0; i < Math.ceil(bytes.length / CHUNK_BYTES); i += 1) this.stored.add(first + i);
      return this.reply(res, 200, { received: this.stored.size });
    }
  }
}

const open = [];
const startServer = () => UploadServer.start();
test.after(async () => {
  await Promise.all(open.map((s) => s.close()));
  assert.deepEqual(open.flatMap((s) => s.violations), [], 'the server saw no malformed run');
});

const fileOf = (data, name = 'big.bin') => new File([data], name);

test('hashes the whole file first, then uploads 16 MiB runs of 512 KiB chunks (last run short)', async () => {
  const data = bytesOf(50 * MIB);
  const server = await startServer();
  const progress = [];
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'art_1', onProgress: (p) => progress.push(p), ...noSleep });
  assert.equal(out.file_sha256, sha(data));
  assert.equal(out.upload_id, server.uploadId('art_1', 'big.bin', data.length, sha(data)), 'the server-derived id');
  const puts = server.events.filter((e) => e[0] === 'put').map((e) => e[1]).sort((a, b) => a - b);
  assert.deepEqual(puts, [0, 32, 64, 96]);
  assert.deepEqual(server.puts.map((p) => p.length).sort((a, b) => b - a), [16 * MIB, 16 * MIB, 16 * MIB, 2 * MIB]);
  assert.equal(server.events[0][0], 'status', 'no run is sent before the status call, which needs the hash');
  assert.equal(server.events[0][1], sha(data), 'status carries the whole-file sha256 (hash before upload)');
  assert.ok(server.maxInFlight <= MAX_RUNS_IN_FLIGHT && server.maxInFlight >= 2, `in flight ${server.maxInFlight}`);
  const phases = progress.map((p) => p.phase);
  assert.ok(phases.indexOf('hashing') >= 0 && phases.indexOf('uploading') > phases.lastIndexOf('hashing'));
  assert.equal(progress.at(-1).phase, 'done');
  assert.equal(progress.at(-1).done, data.length);
  const hashing = progress.filter((p) => p.phase === 'hashing');
  assert.ok(hashing.length >= 3 && hashing.at(-1).done === data.length && hashing.every((p) => p.total === data.length));
});

test('a run is never larger than the cap of 32 chunks, whatever is asked', async () => {
  assert.equal(MAX_CHUNKS_PER_RUN, 32);
  const data = bytesOf(40 * CHUNK_BYTES);
  const server = await startServer();
  await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', chunksPerRequest: 500, ...noSleep });
  assert.ok(server.puts.every((p) => p.length <= 32 * CHUNK_BYTES));
  const small = await startServer();
  await uploadFile(fileOf(data), { client: small.client(), artifactId: 'a', chunksPerRequest: 10, ...noSleep });
  assert.deepEqual(small.puts.map((p) => p.length / CHUNK_BYTES).sort((a, b) => a - b), [10, 10, 10, 10]);
});

test('a 429 run is retried after Retry-After and the file still completes', async () => {
  const data = bytesOf(3 * CHUNK_BYTES);
  const server = await startServer();
  server.failures = [{ at: 0, status: 429, retryAfter: 7 }, { at: 0, status: 503 }];
  const waits = [];
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', sleep: async (ms) => { waits.push(ms); } });
  assert.equal(out.file_sha256, sha(data));
  assert.equal(server.events.filter((e) => e[0] === 'put').length, 3, 'two refusals and one success');
  assert.ok(waits[0] >= 7000, `waited ${waits[0]} for Retry-After 7`);
  assert.ok(waits[1] > 0 && waits.length === 2);
});

test('a 4xx other than 429 is not retried and gives up after the retry budget on persistent 5xx', async () => {
  const data = bytesOf(CHUNK_BYTES);
  const refused = await startServer();
  refused.failures = [{ at: 0, status: 400 }];
  await assert.rejects(uploadFile(fileOf(data), { client: refused.client(), artifactId: 'a', ...noSleep }), (e) => e.status === 400);
  assert.equal(refused.events.filter((e) => e[0] === 'put').length, 1);
  const down = await startServer();
  down.failures = Array.from({ length: 50 }, () => ({ at: 0, status: 502 }));
  await assert.rejects(uploadFile(fileOf(data), { client: down.client(), artifactId: 'a', ...noSleep }), (e) => e.status === 502);
  const tries = down.events.filter((e) => e[0] === 'put').length;
  assert.ok(tries > 1 && tries <= 8, `tries ${tries}`);
});

test('after a simulated reload the file is re-hashed and runs already stored are not sent again', async () => {
  const data = bytesOf(70 * CHUNK_BYTES);                 // runs at 0, 32, 64
  const server = await startServer();
  for (let i = 0; i < 32; i += 1) server.stored.add(i);   // run 0 arrived before the reload
  for (let i = 32; i < 40; i += 1) server.stored.add(i);  // run 32 only half: it is re-sent whole
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', ...noSleep });
  assert.deepEqual(server.events.filter((e) => e[0] === 'put').map((e) => e[1]).sort((a, b) => a - b), [32, 64]);
  assert.equal(out.file_sha256, sha(data));
  const again = await startServer();
  for (let i = 0; i < 70; i += 1) again.stored.add(i);
  await uploadFile(fileOf(data), { client: again.client(), artifactId: 'a', ...noSleep });
  assert.equal(again.events.filter((e) => e[0] === 'put').length, 0, 'a complete upload sends nothing');
});

test('missingToPresent expands inclusive ranges', () => {
  assert.deepEqual([...missingToPresent([[1, 2], [5, 5]], 7)].sort(), [0, 3, 4, 6]);
  assert.deepEqual([...missingToPresent([], 3)].sort(), [0, 1, 2]);
});

test('abort stops hashing and uploading with an AbortError', async () => {
  const data = bytesOf(10 * MIB);
  const controller = new AbortController();
  const server = await startServer();
  const hashing = uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', signal: controller.signal, onProgress: () => controller.abort(), ...noSleep });
  await assert.rejects(hashing, (e) => e.name === 'AbortError');
  assert.equal(server.events.length, 0, 'aborted while hashing: nothing was sent');
  const second = new AbortController();
  const live = await startServer();
  const client = live.client();
  const wrapped = { status: client.status, putRun: async (...args) => { second.abort(); return client.putRun(...args); } };
  await assert.rejects(uploadFile(fileOf(bytesOf(64 * CHUNK_BYTES)), { client: wrapped, artifactId: 'a', signal: second.signal, ...noSleep }), (e) => e.name === 'AbortError');
  assert.ok(live.events.filter((e) => e[0] === 'put').length <= MAX_RUNS_IN_FLIGHT);
});

test('a known file hash skips the hashing pass', async () => {
  const data = bytesOf(CHUNK_BYTES + 5);
  const server = await startServer();
  const phases = [];
  await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', knownSha256: sha(data), onProgress: (p) => phases.push(p.phase), ...noSleep });
  assert.ok(!phases.includes('hashing'));
});

test('an empty file is refused before anything is sent', async () => {
  const server = await startServer();
  await assert.rejects(uploadFile(fileOf(new Uint8Array(0)), { client: server.client(), artifactId: 'a', ...noSleep }), /empty/);
  assert.equal(server.events.length, 0);
});

test('a truncated status is never read as "the rest is present": the listed holes are filled and status is asked again', async () => {
  const data = bytesOf(10 * CHUNK_BYTES);
  const server = await startServer();
  for (const i of [1, 3, 5, 7, 9]) server.stored.add(i);       // holes at 0, 2, 4, 6, 8
  server.cap = 2;                                               // the server lists two holes at a time
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', chunksPerRequest: 1, ...noSleep });
  assert.deepEqual(server.events.filter((e) => e[0] === 'put').map((e) => e[1]).sort((a, b) => a - b), [0, 2, 4, 6, 8]);
  assert.ok(server.statusCalls >= 4, `status asked again (${server.statusCalls})`);
  assert.equal(out.file_sha256, sha(data));
});

test('an upload the server does not call complete is never reported done', async () => {
  const data = bytesOf(2 * CHUNK_BYTES);
  const server = await startServer();
  const client = server.client();
  const lying = { ...client, status: async (...a) => ({ ...(await client.status(...a)), complete: false, missing_ranges: [] }) };
  const phases = [];
  await assert.rejects(uploadFile(fileOf(data), { client: lying, artifactId: 'a', onProgress: (p) => phases.push(p.phase), ...noSleep }), /incomplete/);
  assert.ok(!phases.includes('done'));
  const bad = { ...client, status: async () => ({ upload_id: 'u', total: 2 }) };
  await assert.rejects(uploadFile(fileOf(data), { client: bad, artifactId: 'a', ...noSleep }), /status/);
});

test('at most three runs are in flight across all files uploading at once', async () => {
  let live = 0;
  let peak = 0;
  const wrap = (server) => {
    const c = server.client();
    return { status: c.status, putRun: async (...a) => { live += 1; peak = Math.max(peak, live); try { return await c.putRun(...a); } finally { live -= 1; } } };
  };
  const servers = await Promise.all([startServer(), startServer(), startServer()]);
  await Promise.all(servers.map((sv, i) => uploadFile(fileOf(bytesOf(12 * CHUNK_BYTES), `f${i}.bin`), { client: wrap(sv), artifactId: 'a', chunksPerRequest: 1, ...noSleep })));
  assert.ok(peak <= MAX_RUNS_IN_FLIGHT && peak >= 2, `peak ${peak}`);
});

test('the upload id comes back exactly as the server derives it (vector computed with the python upload_id_for)', async () => {
  const data = bytesOf(VECTOR.size_bytes);
  assert.equal(sha(data), VECTOR.file_sha256, 'the fixture file hashes as the server saw it');
  const server = await startServer();
  const out = await uploadFile(fileOf(data, VECTOR.name), { client: server.client(), artifactId: VECTOR.artifact_id, ...noSleep });
  assert.equal(out.upload_id, VECTOR.upload_id);
});
