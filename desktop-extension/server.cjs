const { createInterface } = require('node:readline');

const endpoint = process.env.GEOSQL_MCP_URL || 'https://geosql.dekart.xyz/mcp';
const token = (process.env.GEOSQL_MCP_TOKEN || '').trim();
let protocolVersion;

// Send only MCP messages to stdout; never expose HTTP bodies or credentials in errors.
function send(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

// GeoSQL uses stateless JSON responses, so each stdio message is one authenticated POST.
async function forward(message) {
  try {
    if (!token) throw new Error('Create a new token in Dekart');
    const headers = {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${token}`
    };
    if (protocolVersion) headers['MCP-Protocol-Version'] = protocolVersion;
    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify(message),
      redirect: 'error'
    });
    if (response.status === 401) throw new Error('Create a new token in Dekart');
    if (response.status === 429) throw new Error('Busy, retry');
    if (!response.ok) throw new Error(`GeoSQL request failed (HTTP ${response.status}). Check the MCP Server URL.`);
    if (response.status === 202 || message.id === undefined) return;
    const reply = await response.json();
    if (message.method === 'initialize' && reply.result) protocolVersion = reply.result.protocolVersion;
    send(reply);
  } catch (error) {
    // Only our fixed messages are safe; fetch errors can contain the configured URL.
    const safeMessage = /^(Create a new token in Dekart|Busy, retry|GeoSQL request failed \(HTTP \d+\)\. Check the MCP Server URL\.)$/.test(error.message)
      ? error.message : 'Cannot reach GeoSQL. Check the MCP Server URL and retry.';
    if (message.id !== undefined) send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: safeMessage } });
    else process.stderr.write(safeMessage + '\n');
  }
}

createInterface({ input: process.stdin }).on('line', line => {
  try {
    const message = JSON.parse(line);
    if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('Invalid MCP message');
    void forward(message);
  } catch {
    send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Invalid JSON-RPC message' } });
  }
});
