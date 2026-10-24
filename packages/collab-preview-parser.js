// Every parse gets a dedicated worker and a hard execution budget. There is no
// synchronous fallback: startup failure, cancellation and timeout fail closed.
export function parsePreview(kind, bytes, { signal } = {}) {
  return new Promise((resolve, reject) => {
    signal?.throwIfAborted();
    const worker = new Worker(new URL('./collab-preview-worker.js', import.meta.url), { type: 'module' });
    let settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer); signal?.removeEventListener('abort', abort);
      worker.terminate();
      if (error) reject(error); else resolve(result);
    };
    const abort = () => finish(signal.reason);
    const timer = setTimeout(() => finish(new Error('Preview parser timed out. Try again or use Download.')), 2000);
    signal?.addEventListener('abort', abort, { once: true });
    worker.onmessage = ({ data }) => {
      if (data.error) finish(new Error(data.error));
      else if (data.result?.kind !== kind) finish(new Error('Invalid preview parser response.'));
      else finish(null, data.result);
    };
    worker.onerror = event => { event.preventDefault(); finish(new Error('Preview parser unavailable.')); };
    worker.onmessageerror = () => finish(new Error('Invalid preview parser response.'));
    try { worker.postMessage({ kind, bytes }, [bytes.buffer]); }
    catch (error) { finish(error); }
  });
}
