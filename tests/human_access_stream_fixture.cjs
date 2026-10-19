// In-memory NDJSON access stream for the real Human page's browser tests.
// The API and data in these tests remain synthetic; no mailbox home is opened.
module.exports = function installHumanAccessStream() {
  const nativeFetch = window.fetch.bind(window);
  window.__emitHumanAccessEvent = (event) => {
    window.__humanAccessController?.enqueue(
      new TextEncoder().encode(JSON.stringify(event) + '\n'));
  };
  window.__closeHumanAccessStream = () => {
    const controller = window.__humanAccessController;
    if (!controller) throw new Error('Human access stream is not active');
    window.__humanAccessController = null;
    controller.close();
  };
  window.fetch = (input, options = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.endsWith('/api/workbench/human/changes')) return nativeFetch(input, options);
    const stream = new ReadableStream({
      start(controller) {
        window.__humanAccessController = controller;
        controller.enqueue(new TextEncoder().encode('{"ready":true}\n'));
        options.signal?.addEventListener('abort', () => {
          if (window.__humanAccessController === controller) window.__humanAccessController = null;
          try { controller.close(); } catch { /* already closed */ }
        }, { once: true });
      },
    });
    return Promise.resolve(new Response(stream, {
      status: 200, headers: { 'Content-Type': 'application/x-ndjson' },
    }));
  };
};
