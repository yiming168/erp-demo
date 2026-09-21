import { createMcpExpressApp } from '@modelcontextprotocol/sdk/server/express.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { createServer } from './server.js';

const origins = new Set(['http://localhost:8080', 'http://127.0.0.1:8080']);

export function createHttpApp(serverFactory = createServer) {
  const app = createMcpExpressApp({ host: '127.0.0.1', allowedHosts: ['127.0.0.1', 'localhost'] });
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    res.setHeader('Vary', 'Origin');
    res.setHeader('Cache-Control', 'no-store');
    const origin = req.headers.origin;
    if (origin !== undefined && !origins.has(origin)) {
      res.status(403).json({ error: 'Origin is not allowed.' });
      return;
    }
    // Native local MCP clients have no Origin. Browser access is limited to llama.cpp.
    if (origin) {
      res.setHeader('Access-Control-Allow-Origin', origin);
      res.setHeader('Access-Control-Allow-Methods', 'POST, GET, DELETE, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Accept, MCP-Protocol-Version, MCP-Session-Id, Last-Event-ID');
      res.setHeader('Access-Control-Expose-Headers', 'MCP-Session-Id, MCP-Protocol-Version');
    }
    if (req.method === 'OPTIONS' && req.path === '/mcp') {
      res.status(204).end();
      return;
    }
    next();
  });
  app.get('/health', (_req, res) => { res.json({ status: 'ok', service: 'erp-readonly-mcp' }); });
  app.post('/mcp', async (req, res) => {
    // No persistent sessions needed for these read-only request/response tools.
    const server = serverFactory();
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    res.on('close', () => { void server.close().catch(() => {}); });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch {
      if (!res.headersSent) res.status(500).json({ jsonrpc: '2.0', id: null,
        error: { code: -32603, message: 'MCP request failed.' } });
      await server.close();
    }
  });
  app.all('/mcp', (_req, res) => {
    res.setHeader('Allow', 'POST, OPTIONS');
    res.status(405).json({ jsonrpc: '2.0', id: null, error: { code: -32000, message: 'Use POST for stateless MCP.' } });
  });
  return app;
}
