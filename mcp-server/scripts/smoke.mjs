import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { fileURLToPath } from 'node:url';

const client = new Client({ name: 'erp-smoke', version: '1.0.0' });
try {
  await client.connect(new StdioClientTransport({ command: process.execPath,
    args: [fileURLToPath(new URL('../dist/index.js', import.meta.url))],
    env: { ERP_BASE_URL: process.env.ERP_BASE_URL ?? 'http://127.0.0.1:5000' } }));
  console.log((await client.listTools()).tools.map(t => t.name).join('\n'));
  if (process.argv[2]) {
    const result = await client.callTool({ name: process.argv[2], arguments: JSON.parse(process.argv[3] ?? '{}') });
    console.log(JSON.stringify(result, null, 2));
    if (result.isError) process.exitCode = 1;
  }
} finally { await client.close(); }
