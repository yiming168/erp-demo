import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { request } from 'node:http';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { createHttpApp } from '../dist/http-app.js';
import { createServer } from '../dist/server.js';

test('HTTP MCP handshake and tool call, CORS allow-list and hostile Host rejection', async () => {
  let calls = 0;
  const app = createHttpApp(() => createServer({ get: async () => { calls++; return [{ contact_id: 7 }]; } }));
  const listener = app.listen(0, '127.0.0.1'); await once(listener, 'listening');
  const url = `http://127.0.0.1:${listener.address().port}/mcp`;
  const client = new Client({ name: 'http-test', version: '1' });
  try {
    for (const origin of ['http://localhost:8080', 'http://127.0.0.1:8080']) {
      const preflight = await fetch(url, { method: 'OPTIONS', headers: { Origin: origin,
        'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,mcp-protocol-version' } });
      assert.equal(preflight.status, 204);
      assert.equal(preflight.headers.get('access-control-allow-origin'), origin);
      assert.ok(preflight.headers.get('access-control-allow-headers').includes('MCP-Protocol-Version'));
    }
    for (const origin of ['https://evil.example', 'null', 'http://localhost:8081']) {
      const response = await fetch(url, { method: 'POST', headers: { Origin: origin } });
      assert.equal(response.status, 403);
      assert.equal(response.headers.get('access-control-allow-origin'), null);
    }
    const hostileHostStatus = await new Promise((resolve, reject) => {
      const req = request(url, { headers: { Host: 'evil.example' } }, res => {
        res.resume(); resolve(res.statusCode);
      });
      req.on('error', reject); req.end();
    });
    assert.equal(hostileHostStatus, 403);
    assert.equal(calls, 0);
    await client.connect(new StreamableHTTPClientTransport(new URL(url), {
      requestInit: { headers: { Origin: 'http://localhost:8080' } }
    }));
    assert.equal((await client.listTools()).tools.length, 6);
    const result = await client.callTool({ name: 'get_customer_contacts', arguments: { client_id: 7 } });
    assert.ok(!result.isError);
    assert.deepEqual(result.structuredContent.rows, [{ contact_id: 7 }]);
    assert.equal(calls, 1);
    assert.equal((await fetch(url)).status, 405);
  } finally { await client.close(); listener.closeAllConnections(); await new Promise(r => listener.close(r)); }
});
