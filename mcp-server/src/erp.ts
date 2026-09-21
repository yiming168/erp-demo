export const MAX_BYTES = 1_048_576;

export function readBaseUrl(value = process.env.ERP_BASE_URL ?? 'http://127.0.0.1:5000'): string {
  const url = new URL(value);
  // Literal loopback only: no DNS resolution or model-controlled host names.
  if (!['http:', 'https:'].includes(url.protocol) ||
      !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('ERP_BASE_URL must be a loopback HTTP(S) origin without credentials, path or query.');
  }
  if (url.port === '8080') throw new Error('Port 8080 is the LLM server, not the Flask ERP API.');
  return url.origin;
}

const allowedPath = /^\/api\/(?:contracts\/[1-9]\d*\/items|clients\/[1-9]\d*\/(?:contacts|shipping_addresses)|products\/[1-9]\d*\/batches)$/;

export class ErpClient {
  private readonly base: string;
  constructor(base = readBaseUrl(), private readonly timeoutMs = 8000) {
    this.base = readBaseUrl(base);
  }
  async get(path: string): Promise<unknown> {
    if (!allowedPath.test(path)) throw new Error('ERP route is not allow-listed.');
    try {
      const response = await fetch(this.base + path, {
        method: 'GET', redirect: 'error', signal: AbortSignal.timeout(this.timeoutMs),
        headers: { Accept: 'application/json' }
      });
      if (!response.ok) {
        await response.body?.cancel();
        throw new Error(`ERP returned HTTP ${response.status}.`);
      }
      if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') ?? '')) {
        await response.body?.cancel();
        throw new Error('ERP returned non-JSON content.');
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error('ERP returned an empty response.');
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > MAX_BYTES) throw new Error('ERP response exceeds the 1 MiB safety limit.');
          chunks.push(value);
        }
      } finally { await reader.cancel(); }
      try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { throw new Error('ERP returned invalid JSON.'); }
    } catch (error) {
      // Never return upstream error bodies, URLs, database exceptions or credentials.
      if (error instanceof Error && /^ERP (returned|response)/.test(error.message)) throw error;
      throw new Error('ERP request failed or timed out. Check the local Flask server.');
    }
  }
}
