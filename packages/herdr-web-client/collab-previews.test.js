import test from 'node:test';
import assert from 'node:assert/strict';
const previews = () => import('../collab-previews.js');
const file = { room_id: 'r', file_id: 'f', relative_path: 'a.msp', status: 'available' };
const source = () => ({ url: 'https://objects.example/private?grant=secret', expires_at: Date.now() / 1000 + 60 });

test('preview registry selects safe renderers by format and falls back without sniffing executable types', async () => {
  const { previewRendererFor } = await previews();
  for (const [name, id] of [['A.MSP', 'msp'], ['a.mzML', 'flasheic'], ['a.mzXML', 'flasheic'], ['a.pdf', 'pdf'], ['a.md', 'markdown'], ['a.markdown', 'markdown'], ['a.json', 'json'], ['a.xml', 'xml'], ['a.yaml', 'yaml'], ['a.yml', 'yaml'], ['a.html', 'unsupported'], ['a.svg', 'unsupported'], ['a.exe', 'unsupported']]) {
    assert.equal(previewRendererFor({ relative_path: name }).id, id, name);
  }
  assert.equal(previewRendererFor({ content_type: 'application/pdf' }).id, 'pdf');
  assert.equal(previewRendererFor({ relative_path: 'x.html', content_type: 'text/markdown' }).id, 'unsupported');
});

test('preview Markdown/JSON/YAML escape hostile content and XML forbids entities and processing instructions', async () => {
  const { textPreviewMarkup } = await import('../collab-preview-text.js');
  const evil = '<img src="https://evil.example/leak" onerror="alert(1)"><script>alert(1)</script>';
  const md = textPreviewMarkup('markdown', `# Title\n${evil}\n[x](javascript:alert(1))`);
  assert.match(md, /<h1>Title<\/h1>/); assert.match(md, /Source/);
  assert.doesNotMatch(md, /<script|<img|href="javascript:/);
  const json = textPreviewMarkup('json', JSON.stringify({ hostile: evil, nested: { value: 1 } }));
  assert.match(json, /<details/); assert.doesNotMatch(json, /<script|<img/);
  const yaml = textPreviewMarkup('yaml', `root:\n  child: ${evil}\n  tag: !!js/function evil\n  alias: &a [*a]`);
  assert.match(yaml, /<details/); assert.doesNotMatch(yaml, /<script|<img/);
  for (const xml of ['<!DOCTYPE x [<!ENTITY x SYSTEM "https://evil.example">]><x>&x;</x>', '<?xml-stylesheet href="https://evil.example"?><x/>']) assert.throws(() => textPreviewMarkup('xml', xml), /XML.*(entities|instructions)/);
  assert.throws(() => textPreviewMarkup('json', '{broken'), /JSON/);
  assert.throws(() => textPreviewMarkup('markdown', 'x'.repeat(200001)), /limit/);
});

test('preview MSP parser handles libraries, metadata and annotated peaks while rejecting malformed or excessive input', async () => {
  const { parseMsp } = await import('../collab-preview-msp.js');
  const spectra = parseMsp('Name: One\nFormula: C2H6\nNum Peaks: 2\n10 5; 20 10 "label"\n\nName: Two\nNum Peaks: 1\n30 2\n');
  assert.equal(spectra.length, 2); assert.equal(spectra[0].name, 'One');
  assert.deepEqual(spectra[0].peaks, [[10, 5], [20, 10]]); assert.deepEqual(spectra[1].peaks, [[30, 2]]);
  assert.deepEqual(spectra[0].metadata, [['Name', 'One'], ['Formula', 'C2H6'], ['Num Peaks', '2']]);
  for (const value of ['Name: Bad\nNum Peaks: 1\nNaN 1', 'Name: Bad\nNum Peaks: 1\n10 -1', 'Name: Bad\nNum Peaks: 2\n10 1', 'Name: Bad\nNum Peaks: 1\n1e309 1', 'x'.repeat(200001)]) assert.throws(() => parseMsp(value), /MSP|limit/);
});

test('preview authorized reads reject expired or unsafe sources, omit credentials and bound streamed bytes', async () => {
  const { readPreviewBytes } = await previews();
  let calls = 0, cancelled = false;
  const fetcher = async (url, init) => {
    calls++; assert.equal(url, source().url); assert.equal(init.credentials, 'omit'); assert.equal(init.referrerPolicy, 'no-referrer'); assert.equal(init.redirect, 'error');
    assert.equal(init.headers?.Authorization, undefined);
    return new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array(200001)); }, cancel() { cancelled = true; } }));
  };
  for (const grant of [{ ...source(), url: 'javascript:alert(1)' }, { ...source(), url: 'https://u:p@objects.example' }, { ...source(), expires_at: 0 }]) await assert.rejects(readPreviewBytes(file, grant, { fetcher }), /authorized|expired|secure/);
  assert.equal(calls, 0);
  await assert.rejects(readPreviewBytes(file, source(), { fetcher }), /limit/); assert.equal(cancelled, true);
  await assert.rejects(readPreviewBytes({ ...file, size_bytes: 200001 }, source(), { fetcher }), /limit/); assert.equal(calls, 1);
  await assert.rejects(readPreviewBytes(file, source(), { fetcher: async () => new Response('', { status: 403 }) }), /403/);
});

test('preview Flasheic adapter sends room/file references only and validates scoped bounded traces', async () => {
  const { loadChromatogram } = await import('../collab-preview-flasheic.js');
  let request;
  const adapter = async ref => { request = ref; return { room_id: 'r', file_id: 'f', traces: [{ name: 'TIC', points: [[0, 2], [1, 5]] }] }; };
  const result = await loadChromatogram({ ...file, url: 'https://public.example', storage_key: 'private' }, { adapter });
  assert.deepEqual(request, { room_id: 'r', file_id: 'f' }); assert.deepEqual(result[0].points, [[0, 2], [1, 5]]);
  await assert.rejects(loadChromatogram(file), /unavailable/);
  for (const response of [{ room_id: 'other', file_id: 'f', traces: [] }, { room_id: 'r', file_id: 'f', traces: [{ points: [[0, Infinity]] }] }]) await assert.rejects(loadChromatogram(file, { adapter: async () => response }), /Invalid/);
});

test('preview jobs are capability gated, scoped and expose derived file records without trusting URLs', async () => {
  const { requestAsyncPreview, pollPreviewJob } = await previews();
  assert.equal((await requestAsyncPreview(file)).status, 'unavailable');
  const job = { room_id: 'r', file_id: 'f', job_id: 'j', status: 'pending' };
  let starts = 0, polls = 0;
  const jobs = { request: async ref => { starts++; assert.deepEqual(ref, { room_id: 'r', file_id: 'f' }); return job; }, status: async ref => {
    polls++; assert.deepEqual(ref, { room_id: 'r', file_id: 'f', job_id: 'j' });
    return { ...job, status: 'ready', artifacts: [{ room_id: 'r', file_id: 'derived', relative_path: 'result.json', status: 'available' }] };
  } };
  const pending = await requestAsyncPreview(file, { jobs });
  const ready = await pollPreviewJob(file, pending, { jobs, interval: 0 });
  assert.equal(starts, 1); assert.equal(polls, 1); assert.equal(ready.artifacts[0].file_id, 'derived');
  await assert.rejects(requestAsyncPreview(file, { jobs: { ...jobs, request: async () => ({ ...job, room_id: 'other' }) } }), /Invalid/);
  await assert.rejects(pollPreviewJob(file, pending, { jobs: { ...jobs, status: async () => ({ ...job, status: 'ready', artifacts: [{ room_id: 'other', file_id: 'x' }] }) }, interval: 0 }), /Invalid/);
  assert.equal((await pollPreviewJob(file, pending, { jobs: { ...jobs, status: async () => ({ ...job, status: 'failed' }) }, interval: 0 })).status, 'failed');
  await assert.rejects(pollPreviewJob(file, pending, { jobs: { ...jobs, status: async () => job }, interval: 0, maxPolls: 2 }), /still pending/);
  await assert.rejects(pollPreviewJob(file, pending, { jobs, signal: AbortSignal.abort(), interval: 0 }), { name: 'AbortError' });
});


test('preview registry treats prototype property names as unsupported extensions and MIME types', async () => {
  const { previewRendererFor } = await previews();
  for (const ext of ['constructor', '__proto__', 'toString']) {
    assert.equal(previewRendererFor({ relative_path: `a.${ext}` }).id, 'unsupported');
    assert.equal(previewRendererFor({ content_type: ext }).id, 'unsupported');
  }
});

test('preview MSP annotations containing colons remain peaks and do not become metadata', async () => {
  const { parseMsp } = await import('../collab-preview-msp.js');
  assert.deepEqual(parseMsp('Name: Annotated\nNum Peaks: 1\n10 2 "label: value"')[0].peaks, [[10, 2]]);
});


test('preview XML worker parser rejects malformed structures without a DOMParser', async () => {
  const { textPreviewMarkup } = await import('../collab-preview-text.js');
  assert.match(textPreviewMarkup('xml', '<root a="&lt;safe&gt;"><item>ok &amp; good</item><![CDATA[<literal>]]></root>'), /ok &amp; good/);
  for (const xml of ['<a><b></a>', '<a/><b/>', '<a x="1" x="2"/>', '<a>&unknown;</a>', '<a x="<bad>"/>', '<a>bad & text</a>', '<a/>tail', '<a><!-- bad--comment --></a>']) {
    assert.throws(() => textPreviewMarkup('xml', xml), /XML/);
  }
});

test('preview worker returns validated results and rejects invalid UTF-8 and byte limits', async () => {
  const { Worker: Thread } = await import('node:worker_threads');
  const { parsePreview } = await import('../collab-preview-parser.js');
  const previous = globalThis.Worker;
  // Adapt the Node thread transport only; run the actual browser worker module.
  globalThis.Worker = class {
    constructor(url) {
      this.thread = new Thread(new URL('data:text/javascript,' + encodeURIComponent(`
        import { parentPort } from 'node:worker_threads';
        globalThis.self = { postMessage: data => parentPort.postMessage(data) };
        await import(${JSON.stringify(url.href)});
        parentPort.on('message', data => self.onmessage({ data }));
      `)));
      this.thread.on('message', data => this.onmessage?.({ data }));
      this.thread.on('error', () => this.onerror?.({ preventDefault() {} }));
    }
    postMessage(data, transfer) { this.thread.postMessage(data, transfer); }
    terminate() { this.thread.terminate(); }
  };
  try {
    for (const [kind, input, expected] of [
      ['markdown', '# Worker heading', /<h1>Worker heading<\/h1>/],
      ['json', '{"key":42}', /42/], ['xml', '<root>safe</root>', /safe/],
      ['yaml', 'root:\n  value: safe', /safe/],
    ]) {
      const result = await parsePreview(kind, new TextEncoder().encode(input));
      assert.equal(result.kind, kind); assert.match(result.markup, expected);
    }
    const msp = await parsePreview('msp', new TextEncoder().encode('Name: Worker\nNum Peaks: 1\n10 2'));
    assert.deepEqual(msp.spectra[0].peaks, [[10, 2]]);
    await assert.rejects(parsePreview('json', new TextEncoder().encode('{bad')), /JSON/);
    await assert.rejects(parsePreview('markdown', new Uint8Array([0xff])), /encoded data|encoding/i);
    await assert.rejects(parsePreview('markdown', new TextEncoder().encode('é'.repeat(100001))), /limit/);
    await assert.rejects(parsePreview('markdown', new Uint8Array(), { signal: AbortSignal.abort() }), { name: 'AbortError' });
  } finally { globalThis.Worker = previous; }
});

test('preview PDF vendor assets exactly match the pinned dependency including font licenses', async () => {
  const { readFile, readdir } = await import('node:fs/promises');
  const root = new URL('../../', import.meta.url);
  const manifest = JSON.parse(await readFile(new URL('node_modules/pdfjs-dist/package.json', root)));
  assert.equal(manifest.version, '6.4.299');
  const copies = [['build/pdf.mjs', 'pdf.mjs'], ['build/pdf.worker.mjs', 'pdf.worker.mjs'], ['LICENSE', 'LICENSE']];
  for (const name of await readdir(new URL('node_modules/pdfjs-dist/standard_fonts/', root))) copies.push([`standard_fonts/${name}`, `standard_fonts/${name}`]);
  for (const [source, target] of copies) assert.deepEqual(await readFile(new URL(`packages/vendor/pdfjs/${target}`, root)), await readFile(new URL(`node_modules/pdfjs-dist/${source}`, root)), target);
});
