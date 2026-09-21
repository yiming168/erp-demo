import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer as httpServer } from 'node:http';
import { once } from 'node:events';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { createServer } from '../dist/server.js';
import { ErpClient, readBaseUrl, MAX_BYTES } from '../dist/erp.js';

test('MCP discovery, mappings, pagination and validation use only approved GET routes', async () => {
  const requests = [];
  const upstream = httpServer((req, res) => {
    requests.push([req.method, req.url]);
    res.setHeader('Content-Type', 'application/json');
    const data = req.url.includes('/contracts/') ? {
      notes: 'demo', items: [{ quantity: '1.00' }], shipments: [],
      invoices: [{ invoice_amount: '12.30', invoice_date: null }],
      receipts: [{ payment_amount: '5.00', payment_date: null }]
    } : req.url.endsWith('/batches') ? [{ batch_type: 'produced', quantity_available: 2, potency_unit: 'billion CFU/g' }]
      : [{ contact_id: 1 }, { contact_id: 2 }];
    res.end(JSON.stringify(data));
  });
  upstream.listen(0, '127.0.0.1'); await once(upstream, 'listening');
  const server = createServer(new ErpClient(`http://127.0.0.1:${upstream.address().port}`));
  const client = new Client({ name: 'test', version: '1' });
  const [a, b] = InMemoryTransport.createLinkedPair();
  try {
    await server.connect(a); await client.connect(b);
    const tools = (await client.listTools()).tools;
    assert.equal(tools.length, 6);
    assert.ok(tools.every(t => t.annotations.readOnlyHint));
    for (const [name, args, suffix] of [
      ['get_customer_contacts', { client_id: 4, limit: 1, offset: 1 }, '/api/clients/4/contacts'],
      ['get_customer_shipping_addresses', { client_id: 4 }, '/api/clients/4/shipping_addresses'],
      ['get_order_details', { contract_id: 3 }, '/api/contracts/3/items'],
      ['get_order_invoices', { contract_id: 3 }, '/api/contracts/3/items'],
      ['get_order_payments', { contract_id: 3 }, '/api/contracts/3/items'],
      ['get_product_inventory', { product_id: 2 }, '/api/products/2/batches']
    ]) {
      const result = await client.callTool({ name, arguments: args });
      assert.ok(!result.isError); assert.deepEqual(requests.at(-1), ['GET', suffix]);
      const data = result.structuredContent;
      if (name === 'get_customer_contacts') {
        assert.equal(data.total, 2); assert.deepEqual(data.rows, [{ contact_id: 2 }]);
      }
      if (name === 'get_order_invoices') assert.equal(data.rows[0].invoice_amount, '12.30');
      if (name === 'get_order_payments') assert.equal(data.rows[0].payment_date, null);
      if (name === 'get_product_inventory') assert.equal(data.rows[0].quantity_unit, 'kg');
    }
    const count = requests.length;
    for (const args of [{ client_id: -1 }, { client_id: '1/../../delete' }, { client_id: 1, limit: 101 },
      { client_id: 1, url: 'https://example.com' }, { client_id: 1.5 }, { client_id: 1, offset: -1 }]) {
      assert.equal((await client.callTool({ name: 'get_customer_contacts', arguments: args })).isError, true);
    }
    assert.equal(requests.length, count);
  } finally { await client.close(); await server.close(); upstream.closeAllConnections(); await new Promise(r => upstream.close(r)); }
});

test('reject unsafe origins and routes', async () => {
  for (const url of ['https://example.com', 'http://localhost:5000', 'http://127.0.0.1:8080',
    'http://user:pass@127.0.0.1', 'http://127.0.0.1/path', 'http://127.0.0.1?x=1']) {
    assert.throws(() => readBaseUrl(url));
  }
  const api = new ErpClient();
  for (const path of ['/delete-order/1', '/api/contracts/1/items?x=1', '//example.com', '/api/clients/../contacts']) {
    await assert.rejects(api.get(path), /allow-listed/);
  }
});

test('HTTP failures, redirects, malformed/oversized JSON and timeouts fail safely', async () => {
  let mode = 'error'; let redirectHits = 0;
  const upstream = httpServer((req, res) => {
    if (req.url === '/secret') redirectHits++;
    if (mode === 'timeout') return;
    if (mode === 'redirect') { res.writeHead(302, { Location: '/secret' }); return res.end(); }
    if (mode === 'error') { res.writeHead(500); return res.end('DB_PASSWORD=secret'); }
    res.setHeader('Content-Type', mode === 'html' ? 'text/html' : 'application/json');
    res.end(mode === 'large' ? JSON.stringify('x'.repeat(MAX_BYTES)) : '<html>secret</html>');
  });
  upstream.listen(0, '127.0.0.1'); await once(upstream, 'listening');
  const api = new ErpClient(`http://127.0.0.1:${upstream.address().port}`, 100);
  try {
    for (mode of ['error', 'redirect', 'html', 'large', 'invalid', 'timeout']) {
      await assert.rejects(api.get('/api/clients/1/contacts'), e => !e.message.includes('secret'));
    }
    assert.equal(redirectHits, 0);
  } finally { upstream.closeAllConnections(); await new Promise(r => upstream.close(r)); }
});

test('invalid upstream schema becomes a tool error without echoing its body', async () => {
  const server = createServer({ get: async () => ({ error: 'secret database internals' }) });
  const client = new Client({ name: 'test', version: '1' });
  const [a,b] = InMemoryTransport.createLinkedPair();
  try {
    await server.connect(a); await client.connect(b);
    const result = await client.callTool({ name: 'get_order_details', arguments: { contract_id: 1 } });
    assert.equal(result.isError, true);
    assert.ok(!JSON.stringify(result).includes('secret'));
  } finally { await client.close(); await server.close(); }
});
