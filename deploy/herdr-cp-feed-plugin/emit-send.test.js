// send() carries the host config's ingest key as X-API-Key (keyed control plane).
//
// Run with:  node --test emit-send.test.js   (Node 18+)
import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { send } from "./emit.js";

async function withIngest(fn) {
  const seen = [];
  const srv = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      seen.push(req.headers["x-api-key"] ?? null);
      res.statusCode = req.headers["x-api-key"] === "k-ingest" ? 200 : 401;
      res.end("{}");
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    await fn(`http://127.0.0.1:${srv.address().port}`, seen);
  } finally {
    await new Promise((r) => srv.close(r));
  }
}

const ENVELOPE = {
  source: "t", entity_type: "host", entity_id: "h",
  event_type: "host.registered", payload: {},
};

test("send() sends X-API-Key when a key is configured", async () => {
  await withIngest(async (url, seen) => {
    await send(url, ENVELOPE, "k-ingest");
    assert.deepEqual(seen, ["k-ingest"]);
  });
});

test("send() sends no X-API-Key header when no key is configured", async () => {
  await withIngest(async (url, seen) => {
    await send(url, ENVELOPE, null);
    await send(url, ENVELOPE);
    assert.deepEqual(seen, [null, null]);
  });
});
