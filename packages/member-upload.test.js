import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { uploadFile, CHUNK_BYTES, MAX_CHUNKS_PER_RUN, MAX_RUNS_IN_FLIGHT, missingToPresent } from './member-upload.js';

const MIB = 1024 * 1024;
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const noSleep = { sleep: async () => {} };

function bytesOf(size) {
  const data = new Uint8Array(size);
  for (let i = 0; i < size; i += 1) data[i] = (i * 31 + (i >> 8)) & 255;
  return data;
}

function httpError(status, extra = {}) {
  return Object.assign(new Error(`http ${status}`), { status, ...extra });
}

/** A client that behaves like the server: the upload id derives from the whole-file sha256, runs are stored by chunk. */
class FakeServer {
  constructor() { this.stored = new Set(); this.events = []; this.puts = []; this.inFlight = 0; this.maxInFlight = 0; this.failures = []; }
  uploadId(artifactId, name, size, fileSha) { return `u_${sha(`${artifactId}|${name}|${size}|${fileSha}`).slice(0, 32)}`; }
  client() {
    return {
      status: async (artifactId, args) => {
        this.events.push(['status', args.file_sha256]);
        const total = Math.ceil(args.size_bytes / CHUNK_BYTES);
        const ranges = [];
        let start = null;
        for (let i = 0; i <= total; i += 1) {
          const absent = i < total && !this.stored.has(i);
          if (absent && start === null) start = i;
          if (!absent && start !== null) { ranges.push([start, i - 1]); start = null; }
        }
        return { upload_id: this.uploadId(artifactId, args.name, args.size_bytes, args.file_sha256), total, missing_ranges: ranges, complete: ranges.length === 0 };
      },
      putRun: async (artifactId, uploadId, first, bytes, meta) => {
        this.events.push(['put', first]);
        this.inFlight += 1;
        this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
        try {
          await new Promise((resolve) => setImmediate(resolve));
          const failure = this.failures.shift();
          if (failure && failure.at === first) throw failure.error;
          if (failure) this.failures.unshift(failure);
          assert.equal(meta.run_sha256, sha(bytes), 'run sha256 header matches the body');
          assert.equal(uploadId, this.uploadId(artifactId, meta.name, meta.size_bytes, meta.file_sha256));
          this.puts.push({ first, length: bytes.length, signal: meta.signal });
          for (let i = 0; i < Math.ceil(bytes.length / CHUNK_BYTES); i += 1) this.stored.add(first + i);
          return { received: this.stored.size };
        } finally { this.inFlight -= 1; }
      },
    };
  }
}

const fileOf = (data, name = 'big.bin') => new File([data], name);

test('hashes the whole file first, then uploads 16 MiB runs of 512 KiB chunks (last run short)', async () => {
  const data = bytesOf(50 * MIB);
  const server = new FakeServer();
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
  const server = new FakeServer();
  await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', chunksPerRequest: 500, ...noSleep });
  assert.ok(server.puts.every((p) => p.length <= 32 * CHUNK_BYTES));
  const small = new FakeServer();
  await uploadFile(fileOf(data), { client: small.client(), artifactId: 'a', chunksPerRequest: 10, ...noSleep });
  assert.deepEqual(small.puts.map((p) => p.length / CHUNK_BYTES).sort((a, b) => a - b), [10, 10, 10, 10]);
});

test('a 429 run is retried after Retry-After and the file still completes', async () => {
  const data = bytesOf(3 * CHUNK_BYTES);
  const server = new FakeServer();
  server.failures = [{ at: 0, error: httpError(429, { retryAfter: 7 }) }, { at: 0, error: httpError(503) }];
  const waits = [];
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', sleep: async (ms) => { waits.push(ms); } });
  assert.equal(out.file_sha256, sha(data));
  assert.equal(server.events.filter((e) => e[0] === 'put').length, 3, 'two refusals and one success');
  assert.ok(waits[0] >= 7000, `waited ${waits[0]} for Retry-After 7`);
  assert.ok(waits[1] > 0 && waits.length === 2);
});

test('a 4xx other than 429 is not retried and gives up after the retry budget on persistent 5xx', async () => {
  const data = bytesOf(CHUNK_BYTES);
  const refused = new FakeServer();
  refused.failures = [{ at: 0, error: httpError(400) }];
  await assert.rejects(uploadFile(fileOf(data), { client: refused.client(), artifactId: 'a', ...noSleep }), (e) => e.status === 400);
  assert.equal(refused.events.filter((e) => e[0] === 'put').length, 1);
  const down = new FakeServer();
  down.failures = Array.from({ length: 50 }, () => ({ at: 0, error: httpError(502) }));
  await assert.rejects(uploadFile(fileOf(data), { client: down.client(), artifactId: 'a', ...noSleep }), (e) => e.status === 502);
  const tries = down.events.filter((e) => e[0] === 'put').length;
  assert.ok(tries > 1 && tries <= 8, `tries ${tries}`);
});

test('after a simulated reload the file is re-hashed and runs already stored are not sent again', async () => {
  const data = bytesOf(70 * CHUNK_BYTES);                 // runs at 0, 32, 64
  const server = new FakeServer();
  for (let i = 0; i < 32; i += 1) server.stored.add(i);   // run 0 arrived before the reload
  for (let i = 32; i < 40; i += 1) server.stored.add(i);  // run 32 only half: it is re-sent whole
  const out = await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', ...noSleep });
  assert.deepEqual(server.events.filter((e) => e[0] === 'put').map((e) => e[1]).sort((a, b) => a - b), [32, 64]);
  assert.equal(out.file_sha256, sha(data));
  const again = new FakeServer();
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
  const server = new FakeServer();
  const hashing = uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', signal: controller.signal, onProgress: () => controller.abort(), ...noSleep });
  await assert.rejects(hashing, (e) => e.name === 'AbortError');
  assert.equal(server.events.length, 0, 'aborted while hashing: nothing was sent');
  const second = new AbortController();
  const live = new FakeServer();
  const client = live.client();
  const wrapped = { status: client.status, putRun: async (...args) => { second.abort(); return client.putRun(...args); } };
  await assert.rejects(uploadFile(fileOf(bytesOf(64 * CHUNK_BYTES)), { client: wrapped, artifactId: 'a', signal: second.signal, ...noSleep }), (e) => e.name === 'AbortError');
  assert.ok(live.events.filter((e) => e[0] === 'put').length <= MAX_RUNS_IN_FLIGHT);
});

test('a known file hash skips the hashing pass', async () => {
  const data = bytesOf(CHUNK_BYTES + 5);
  const server = new FakeServer();
  const phases = [];
  await uploadFile(fileOf(data), { client: server.client(), artifactId: 'a', knownSha256: sha(data), onProgress: (p) => phases.push(p.phase), ...noSleep });
  assert.ok(!phases.includes('hashing'));
});

test('an empty file is refused before anything is sent', async () => {
  const server = new FakeServer();
  await assert.rejects(uploadFile(fileOf(new Uint8Array(0)), { client: server.client(), artifactId: 'a', ...noSleep }), /empty/);
  assert.equal(server.events.length, 0);
});
