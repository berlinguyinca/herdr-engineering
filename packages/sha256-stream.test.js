import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { createSha256 } from './sha256-stream.js';

const hex = (bytes) => Buffer.from(bytes).toString('hex');
const sha = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const feed = (parts) => { const h = createSha256(); for (const p of parts) h.update(p); return h.digestHex(); };

test('NIST vectors', () => {
  assert.equal(feed([]), 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  assert.equal(feed([new TextEncoder().encode('abc')]), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(feed([new TextEncoder().encode('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')]),
    '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
  assert.equal(feed([new TextEncoder().encode('a'.repeat(1_000_000))]),
    'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0');
});

test('matches crypto for random buffers split at random boundaries', () => {
  for (let round = 0; round < 60; round += 1) {
    const data = crypto.randomBytes(Math.floor(Math.random() * 5000));
    const parts = [];
    for (let at = 0; at < data.length;) {
      const take = Math.floor(Math.random() * 200);
      parts.push(new Uint8Array(data.subarray(at, at + take)));
      at += take;
      if (take === 0 && Math.random() < 0.5) at += 1, parts.push(new Uint8Array(data.subarray(at - 1, at)));
    }
    assert.equal(feed(parts), sha(data), `round ${round}`);
  }
});

test('block-boundary lengths (55, 56, 63, 64, 65, 119, 120, 128) are right', () => {
  for (const n of [55, 56, 63, 64, 65, 119, 120, 128]) {
    const data = crypto.randomBytes(n);
    assert.equal(feed([new Uint8Array(data)]), sha(data), String(n));
  }
});

test('update does not retain or modify the caller buffer, and digest is repeatable', () => {
  const data = new Uint8Array([1, 2, 3]);
  const h = createSha256();
  h.update(data);
  assert.deepEqual([...data], [1, 2, 3]);
  const first = h.digestHex();
  assert.equal(h.digestHex(), first);
  assert.equal(first, sha(Buffer.from([1, 2, 3])));
  assert.throws(() => h.update(new Uint8Array(1)), /finished/);
  assert.equal(hex(new Uint8Array(0)), '');
});
