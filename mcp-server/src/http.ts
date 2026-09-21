import { createHttpApp } from './http-app.js';
import { readBaseUrl } from './erp.js';

readBaseUrl();
const listener = createHttpApp().listen(3001, '127.0.0.1', () => {
  console.error('ERP MCP HTTP ready: http://127.0.0.1:3001/mcp');
});
listener.on('error', () => {
  console.error('Could not listen on 127.0.0.1:3001. Check whether the port is already in use.');
  process.exitCode = 1;
});
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    listener.close();
    listener.closeAllConnections();
  });
}
