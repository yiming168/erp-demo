import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { createServer } from './server.js';

try {
  await createServer().connect(new StdioServerTransport());
  console.error('ERP read-only MCP ready (stdio).');
} catch {
  console.error('MCP startup failed. Check ERP_BASE_URL; use a loopback ERP origin, normally port 5000.');
  process.exitCode = 1;
}
