const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { createServer } = require('node:http');
const { spawn, execFileSync } = require('node:child_process');
const { mkdtempSync, readFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { createInterface } = require('node:readline');

const directory = mkdtempSync(join(tmpdir(), 'geosql-extension-'));
let server, url, manifest;
const requests = [];

before(async () => {
  execFileSync('python3', ['scripts/build_mcpb.py']);
  execFileSync('python3', ['-m', 'zipfile', '-e', 'dekart/geosql.mcpb', directory]);
  manifest = JSON.parse(readFileSync(join(directory, 'manifest.json')));
  server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const message = JSON.parse(body);
    requests.push({ headers: req.headers, message, path: req.url });
    if (req.headers.authorization === 'Bearer revoked') {
      res.writeHead(401).end('private upstream detail');
    } else if (message.method === 'tools/call') {
      res.writeHead(429).end('private upstream detail');
    } else if (message.id === undefined) {
      res.writeHead(202).end();
    } else {
      res.setHeader('Content-Type', 'application/json');
      const result = message.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'geosql', version: '1' } }
        : { tools: [{ name: 'get_report_url', inputSchema: { type: 'object' } }] };
      res.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }));
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  url = `http://127.0.0.1:${server.address().port}/mcp`;
});

after(async () => {
  await new Promise(resolve => server.close(resolve));
  rmSync(directory, { recursive: true, force: true });
});

// Launch the installed bundle using the same manifest substitutions as Desktop.
function launch(token = 'test-secret') {
  const config = manifest.server.mcp_config;
  const values = { '${__dirname}': directory, '${user_config.url}': url, '${user_config.token}': token };
  const substitute = value => value.replace(/\$\{[^}]+\}/g, key => values[key]);
  const child = spawn(config.command, config.args.map(substitute), {
    env: { ...process.env, ...Object.fromEntries(Object.entries(config.env).map(([key, value]) => [key, substitute(value)])) }
  });
  const queue = [], waiters = [];
  let errors = '';
  child.stderr.on('data', chunk => { errors += chunk; });
  createInterface({ input: child.stdout }).on('line', line => {
    const message = JSON.parse(line);
    if (waiters.length) waiters.shift()(message);
    else queue.push(message);
  });
  return {
    child,
    send: message => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n'),
    next: () => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => waiters.push(resolve)),
    errors: () => errors
  };
}

test('installed bundle initializes, lists tools, and forwards bearer and protocol without OAuth', { timeout: 10000 }, async t => {
  const bridge = launch();
  t.after(() => bridge.child.kill());
  assert.equal(manifest.user_config.token.sensitive, true);
  assert.equal(manifest.user_config.url.default, 'https://geosql.dekart.xyz/mcp');
  bridge.send({ id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } } });
  assert.equal((await bridge.next()).result.serverInfo.name, 'geosql');
  bridge.send({ method: 'notifications/initialized' });
  bridge.send({ id: 2, method: 'tools/list' });
  assert.equal((await bridge.next()).result.tools[0].name, 'get_report_url');
  const sent = requests.filter(item => item.headers.authorization === 'Bearer test-secret');
  assert.deepEqual(sent.map(item => item.message.method).sort(), ['initialize', 'notifications/initialized', 'tools/list'].sort());
  assert.ok(sent.every(item => item.path === '/mcp' && item.headers.accept.includes('text/event-stream')));
  assert.equal(sent.find(item => item.message.method === 'tools/list').headers['mcp-protocol-version'], '2025-06-18');
  assert.equal(bridge.errors(), '');
});

test('missing token gives actionable error without contacting the server', { timeout: 10000 }, async t => {
  const bridge = launch('');
  t.after(() => bridge.child.kill());
  const count = requests.length;
  bridge.send({ id: 0, method: 'initialize' });
  assert.equal((await bridge.next()).error.message, 'Create a new token in Dekart');
  assert.equal(requests.length, count);
});

test('429 says Busy, retry and does not disclose upstream content', { timeout: 10000 }, async t => {
  const bridge = launch();
  t.after(() => bridge.child.kill());
  bridge.send({ id: 3, method: 'tools/call', params: { name: 'run_query' } });
  assert.equal((await bridge.next()).error.message, 'Busy, retry');
  assert.equal(bridge.errors(), '');
});

test('revoked token asks for a new token without leaking credentials', { timeout: 10000 }, async t => {
  const bridge = launch('revoked');
  t.after(() => bridge.child.kill());
  bridge.send({ id: 4, method: 'tools/list' });
  assert.equal((await bridge.next()).error.message, 'Create a new token in Dekart');
  assert.equal(bridge.errors(), '');
});
