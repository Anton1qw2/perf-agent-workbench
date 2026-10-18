// Exercise the real UI script with controlled HTTP scheduling. No browser dependency.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID, webcrypto } = require('node:crypto');
const web = path.join(__dirname, '../src/perf_chat/ui/web');
const deferred = () => {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
};
const settle = async () => { for (let i = 0; i < 15; i++) await new Promise(setImmediate); };

class Element {
  constructor() {
    this.value = ''; this.hidden = false; this.disabled = false; this.textContent = '';
    this.children = []; this.attributes = {}; this.scrollTop = 0;
    this.scrollHeight = 200; this.clientHeight = 200;
    this.classList = { toggle() {} };
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  contains(element) { return element === this; }
  focus() {}
}

async function fixture(initialStorage = {}, cryptoApi = {randomUUID}) {
  const elements = Object.fromEntries([...fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    .matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const conversations = Object.fromEntries(['a', 'b', 'c'].map(id => [id, {
    id, messages: [], runs: [], busy: false, missing_fields: [],
    spec: { spec_version: 1, summary: 'task', target: {repository: 'repo', branch: 'main', profile: null} }
  }]));
  const connections = [], requests = [], timers = new Map();
  let timerId = 0;
  let intercept;
  const storage = new Map(Object.entries({'perf-chat-conversation': 'a', ...initialStorage}));
  const response = (body, status = 200) => ({ok: status < 400, status, json: async () => structuredClone(body)});
  const context = vm.createContext({
    structuredClone, crypto: cryptoApi, console,
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    window: { matchMedia: () => ({matches: false}) },
    document: {
      getElementById: id => elements[id], activeElement: null, body: new Element(),
      createElement: () => new Element(), createTextNode: text => text, addEventListener() {}
    },
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; connections.push(this); }
      close() { this.closed = true; }
      addEventListener(name, fn) { this[name] = fn; }
    },
    fetch: async (url, options = {}) => {
      const request = {url, method: options.method || 'GET', body: options.body && JSON.parse(options.body)};
      requests.push(request);
      if (intercept) {
        const result = intercept(request, response);
        if (result !== undefined) return await result;
      }
      if (url === '/api/health') return response({mode: 'demo'});
      if (url === '/api/conversations') return response(Object.values(conversations));
      const id = url.split('/')[3];
      if (request.method === 'GET') return response(conversations[id]);
      if (request.method === 'PUT') {
        conversations[id].spec = structuredClone(request.body.spec);
        conversations[id].spec.spec_version++;
        conversations[id].credentials ||= {};
        for (const key of ['git_token', 'profiler_token']) {
          if (key in (request.body.credentials || {})) conversations[id].credentials[key + '_configured'] = !!request.body.credentials[key];
        }
      }
      return response({});
    }
  });
  vm.runInContext(fs.readFileSync(path.join(web, 'app.js'), 'utf8'), context);
  await settle();
  return {
    elements, conversations, connections, requests, response, timers, storage,
    intercept: fn => { intercept = fn; },
    run: code => vm.runInContext(code, context),
    submit: id => elements[id].onsubmit({preventDefault() {}})
  };
}

test('late submit never clears another conversation draft', async () => {
  const app = await fixture(), pending = deferred();
  app.intercept(request => request.method === 'POST' ? pending.promise : undefined);
  app.elements.input.value = 'message for a'; app.submit('composer'); await settle();
  await app.run("open('b')");
  app.elements.input.value = 'draft for b';
  pending.resolve(app.response({})); await settle();
  assert.equal(app.elements.input.value, 'draft for b');
  assert.equal(app.requests.find(r => r.method === 'POST').url, '/api/conversations/a/messages');
  await app.run("open('a')");
  assert.equal(app.elements.input.value, '');
  await app.run("open('b')");
  assert.equal(app.elements.input.value, 'draft for b');
});

test('tokens are sent separately, cleared after save and never stored in drafts', async () => {
  const f = await fixture();
  f.elements['git-token'].value = 'synthetic-web-token';
  f.elements['context-form'].oninput();
  f.submit('context-form');
  await settle();
  const saved = f.requests.find(r => r.method === 'PUT');
  assert.equal(saved.body.credentials.git_token, 'synthetic-web-token');
  assert.ok(!JSON.stringify(saved.body.spec).includes('synthetic-web-token'));
  assert.equal(f.elements['git-token'].value, '');
  assert.equal(f.elements['git-token-status'].textContent, 'Saved on backend');
  assert.ok(!JSON.stringify(f.conversations).includes('synthetic-web-token'));
  f.elements['profiler-token'].value = 'synthetic-unsaved-token';
  f.elements['context-form'].oninput();
  await f.run('open("b")');
  assert.equal(f.elements['profiler-token'].value, '');
  assert.ok(!f.run('JSON.stringify([...drafts])').includes('synthetic-unsaved-token'));
});

test('blank token fields preserve saved credentials and Remove explicitly clears', async () => {
  const f = await fixture();
  f.conversations.a.credentials = {profiler_token_configured: true};
  await f.run('refresh()');
  f.elements.branch.value = 'feature';
  f.elements['context-form'].oninput();
  f.submit('context-form');
  await settle();
  assert.deepEqual(f.requests.find(r => r.method === 'PUT').body.credentials, {});
  f.elements['remove-profiler-token'].onclick();
  f.submit('context-form');
  await settle();
  assert.equal(f.requests.filter(r => r.method === 'PUT').at(-1).body.credentials.profiler_token, '');
  assert.equal(f.elements['profiler-token-status'].textContent, 'Not configured');
});

test('a failed snapshot after the final SSE event retries without another event', async () => {
  const app = await fixture();
  app.conversations.a.runs = [{id: 'r', status: 'demo', task: {spec_version: 1}, result: {summary: 'done'}}];
  let failOnce = true;
  app.intercept(r => {
    if (r.url === '/api/conversations/a' && failOnce) {
      failOnce = false;
      return app.response({detail: 'temporary'}, 503);
    }
  });
  await app.connections.at(-1).update();
  assert.equal(app.timers.size, 1);
  await [...app.timers.values()][0]();
  assert.match(app.elements.execution.textContent, /done/);
});

test('racing navigation leaves one live SSE for the selected conversation', async () => {
  const app = await fixture(), slow = deferred();
  app.intercept(r => r.url === '/api/conversations/b' ? slow.promise : undefined);
  const openingB = app.run("open('b')"); await settle();
  await app.run("open('c')");
  slow.resolve(app.response(app.conversations.b)); await openingB;
  const live = app.connections.filter(c => !c.closed);
  assert.equal(live.length, 1);
  assert.equal(live[0].url, '/api/conversations/c/events');
});

test('dirty context rebases across a chat update without replacing the task', async () => {
  const app = await fixture();
  app.elements.branch.value = 'feature/perf'; app.elements['context-form'].oninput();
  app.conversations.a.spec.spec_version = 2;
  app.conversations.a.spec.summary = 'new agreed task';
  await app.run('refresh()');
  app.submit('context-form'); await settle();
  const write = app.requests.find(r => r.method === 'PUT');
  assert.equal(write.body.expected_version, 2);
  assert.equal(write.body.spec.summary, 'new agreed task');
  assert.equal(write.body.spec.target.branch, 'feature/perf');
});

test('conflicting project metadata requires explicit reload and keeps the draft', async () => {
  const app = await fixture();
  app.elements.branch.value = 'my-draft'; app.elements['context-form'].oninput();
  app.conversations.a.spec.spec_version = 2;
  app.conversations.a.spec.target.branch = 'changed-in-tui';
  app.submit('context-form'); await settle();
  assert.equal(app.requests.filter(r => r.method === 'PUT').length, 0);
  assert.equal(app.elements.branch.value, 'my-draft');
  assert.equal(app.elements['reload-context'].hidden, false);
  await app.elements['reload-context'].onclick();
  assert.equal(app.elements.branch.value, 'changed-in-tui');
});


test('settings groups collapse independently and restore their saved state', async () => {
  const f = await fixture({'perf-chat-repository-settings-open': 'false'});
  assert.equal(f.elements['repository-settings'].open, false);
  assert.equal(f.elements['profiler-settings'].open, true);
  f.elements['profiler-settings'].open = false;
  f.elements['profiler-settings'].ontoggle();
  assert.equal(f.storage.get('perf-chat-profiler-settings-open'), 'false');
  await f.run('open("b")');
  assert.equal(f.elements['repository-settings'].open, false);
  assert.equal(f.elements['profiler-settings'].open, false);
});


test('HTTP-compatible request IDs support Send and Run without randomUUID', async () => {
  const f = await fixture({}, {getRandomValues: bytes => webcrypto.getRandomValues(bytes)});
  f.elements.input.value = 'Optimize allocations';
  f.submit('composer');
  await settle();
  f.elements.run.onclick();
  await settle();
  const commands = f.requests.filter(r => r.method === 'POST');
  assert.equal(commands.length, 2);
  assert.equal(commands[0].url, '/api/conversations/a/messages');
  assert.equal(commands[1].url, '/api/conversations/a/runs');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  for (const request of commands) assert.match(request.body.request_id, uuid);
  assert.notEqual(commands[0].body.request_id, commands[1].body.request_id);
});

test('fallback request ID is reused after an uncertain message failure', async () => {
  const f = await fixture({}, {getRandomValues: bytes => webcrypto.getRandomValues(bytes)});
  let failed = false;
  f.intercept(request => {
    if (request.method === 'POST' && !failed) {
      failed = true;
      return Promise.reject(new Error('Connection lost'));
    }
  });
  f.elements.input.value = 'Optimize allocations';
  f.submit('composer'); await settle();
  f.submit('composer'); await settle();
  const commands = f.requests.filter(r => r.method === 'POST');
  assert.equal(commands.length, 2);
  assert.equal(commands[0].body.request_id, commands[1].body.request_id);
});

test('missing Web Crypto produces an actionable error without sending a command', async () => {
  const f = await fixture({}, undefined);
  f.run('globalThis.crypto = undefined');
  f.elements.input.value = 'Optimize allocations';
  f.submit('composer'); await settle();
  assert.match(f.elements.error.textContent, /Web Crypto support/);
  assert.equal(f.requests.filter(r => r.method === 'POST').length, 0);
  assert.equal(f.elements.input.value, 'Optimize allocations');
});
