const $ = id => document.getElementById(id);
let cid = localStorage.getItem('perf-chat-conversation');
let state, stream, contextSpec, contextVersion, dirty = false, refreshSequence = 0;
let navigationSequence = 0;
let errorSource = '';
let streamCleanup = () => {};
const drafts = new Map();
const pendingMessages = new Set();
const messageRequests = new Map();
const contextKeys = ['repository', 'branch', 'profile'];
const tokenFields = [['git-token', 'git_token'], ['profiler-token', 'profiler_token']];
const tokenRemovals = new Set();

function renderCredentials() {
  for (const [id, key] of tokenFields) {
    const configured = !!state?.credentials?.[key + '_configured'];
    $(id).placeholder = configured ? 'Saved — enter to replace' : 'Enter token';
    $(id + '-status').textContent = tokenRemovals.has(key) ? 'Will remove on Save' : configured ? 'Saved on backend' : 'Not configured';
    $('remove-' + id).hidden = !configured || tokenRemovals.has(key);
  }
}

function rememberDraft() {
  if (!cid) return;
  if (!state || state.id !== cid) {
    drafts.set(cid, {...drafts.get(cid), text: $('input').value});
    return;
  }
  drafts.set(cid, {
    text: $('input').value,
    dirty: dirty && contextKeys.some(key => ($(key).value.trim() || null) !== (contextSpec?.target[key] ?? null)),
    contextSpec, contextVersion,
    fields: Object.fromEntries(contextKeys.map(key => [key, $(key).value]))
  });
}
const statusNames = {
  queued: 'Queued', running: 'Running', waiting: 'Awaiting reply',
  cancelling: 'Cancelling…', cancelled: 'Cancelled', failed: 'Error',
  succeeded: 'Completed', demo: 'Demo completed'
};

function setContextOpen(open) {
  if (open && window.matchMedia('(max-width: 850px)').matches) setDialogsOpen(false);
  document.body.classList.toggle('context-open', open);
  $('context-content').hidden = !open;
  $('toggle-context').setAttribute('aria-expanded', String(open));
  $('toggle-context').setAttribute('aria-label', (open ? 'Hide' : 'Open') + ' project settings');
  $('toggle-context').title = open
    ? 'Hide sidebar (Esc)'
    : 'Open sidebar: repository, branch and profile';
  localStorage.setItem('perf-chat-context-open', String(open));
  if (!open && $('context-panel').contains(document.activeElement)) $('toggle-context').focus();
}

function setDialogsOpen(open) {
  if (open && window.matchMedia('(max-width: 850px)').matches) setContextOpen(false);
  document.body.classList.toggle('dialogs-open', open);
  $('dialogs-content').hidden = !open;
  $('toggle-dialogs').setAttribute('aria-expanded', String(open));
  $('toggle-dialogs').setAttribute('aria-label', (open ? 'Hide' : 'Open') + ' chats');
  $('toggle-dialogs').title = open ? 'Collapse chats (Esc)' : 'Open chats';
  localStorage.setItem('perf-chat-dialogs-open', String(open));
  if (!open && $('dialogs-panel').contains(document.activeElement)) $('toggle-dialogs').focus();
}

function showError(error, source = 'request') {
  errorSource = source;
  $('error').textContent = typeof error === 'string' ? error : error.message;
  $('error').hidden = false;
}

async function api(path, method = 'GET', body) {
  const response = await fetch('/api' + path, {
    method, headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

function latest() { return state?.runs[0]; }

async function sidebar() {
  const current = cid;
  const conversations = await api('/conversations');
  if (current !== cid) return;
  $('conversations').replaceChildren(...conversations.map(c => {
    const button = document.createElement('button');
    button.textContent = c.spec.summary || 'New chat';
    button.className = c.id === cid ? 'active' : '';
    button.onclick = () => guard(() => open(c.id));
    return button;
  }));
}

function renderContext() {
  contextVersion = state.spec.spec_version;
  contextSpec = structuredClone(state.spec);
  for (const key of contextKeys) {
    $(key).value = contextSpec.target[key] ?? '';
  }
  renderCredentials();
}

async function refresh() {
  const current = cid, sequence = ++refreshSequence;
  const data = await api('/conversations/' + current);
  if (cid !== current || sequence !== refreshSequence) return;
  state = data;
  if (state.last_error) showError(state.last_error.message, 'job');
  else if (errorSource === 'job') { $('error').hidden = true; errorSource = ''; }
  const nearBottom = $('messages').scrollHeight - $('messages').clientHeight - $('messages').scrollTop < 40;
  const scrollTop = $('messages').scrollTop;
  $('messages').replaceChildren();
  if (!state.messages.length) {
    const div = document.createElement('div'); div.className = 'welcome';
    const h = document.createElement('h2'); h.textContent = 'Start with your task';
    const p = document.createElement('p');
    p.textContent = 'Describe the problem in your own words. We will clarify goals, constraints and validation in the chat.';
    div.append(h, p); $('messages').append(div);
  }
  for (const message of state.messages) {
    const div = document.createElement('div'); div.className = 'message ' + message.role;
    const role = document.createElement('span'); role.className = 'role';
    role.textContent = ({user: 'You', executor: 'Executor'})[message.role] || 'Assistant';
    div.append(role, document.createTextNode(message.content)); $('messages').append(div);
  }
  $('messages').scrollTop = nearBottom ? $('messages').scrollHeight : scrollTop;
  if (!dirty) renderContext();
  else renderCredentials();
  const run = latest();
  const active = run && ['queued', 'running', 'waiting', 'cancelling'].includes(run.status);
  $('status').textContent = state.busy ? 'Agent is replying…' : (run ? statusNames[run.status] : 'Ready');
  $('run').disabled = dirty || state.busy || !!active || state.missing_fields.length > 0;
  $('run').title = dirty ? 'Save project settings before running'
    : state.missing_fields.length ? 'Clarify the task in chat and provide a repository and branch' : '';
  $('cancel').disabled = !active || run.status === 'cancelling';
  $('send').disabled = state.busy || pendingMessages.has(cid);
  $('question').hidden = run?.status !== 'waiting';
  $('question').textContent = run?.clarification?.question || '';
  $('execution').textContent = run ? `${statusNames[run.status]} · ${run.result?.summary || run.progress || ''}` : '';
}

async function open(id) {
  const navigation = ++navigationSequence;
  rememberDraft();
  streamCleanup(); stream = undefined;
  cid = id; state = undefined; dirty = false;
  tokenRemovals.clear();
  for (const [id] of tokenFields) $(id).value = '';
  $('send').disabled = true; $('run').disabled = true; $('cancel').disabled = true;
  $('save-context').disabled = true;
  $('input').value = drafts.get(id)?.text || '';
  localStorage.setItem('perf-chat-conversation', id);
  $('error').hidden = true; $('context-status').textContent = '';
  await refresh();
  if (navigation !== navigationSequence) return;
  const draft = drafts.get(id);
  if (draft?.dirty) {
    ({contextSpec, contextVersion} = draft);
    for (const key of contextKeys) $(key).value = draft.fields[key];
    dirty = true;
    $('context-status').textContent = 'Unsaved changes restored';
  }
  $('save-context').disabled = false;
  await sidebar();
  if (navigation !== navigationSequence) return;
  const connection = new EventSource('/api/conversations/' + id + '/events');
  stream = connection;
  let refreshing = false, requested = false, retryTimer;
  const currentConnection = () => stream === connection && cid === id;
  const synchronize = async () => {
    requested = true;
    if (refreshing || !currentConnection()) return;
    refreshing = true;
    clearTimeout(retryTimer);
    try {
      do {
        requested = false;
        await refresh();
      } while (requested && currentConnection());
    } catch (error) {
      if (currentConnection()) {
        $('status').textContent = 'Syncing again…';
        retryTimer = setTimeout(synchronize, 1000);
      }
    } finally { refreshing = false; }
  };
  streamCleanup = () => { connection.close(); clearTimeout(retryTimer); };
  connection.addEventListener('update', synchronize);
  connection.onopen = synchronize;
  connection.onerror = () => {
    if (stream === connection && cid === id) $('status').textContent = 'Reconnecting…';
  };
}

async function guard(fn) {
  try { await fn(); } catch (error) { showError(error); }
}

$('toggle-context').onclick = () => {
  const opening = $('context-content').hidden;
  setContextOpen(opening);
  if (opening) $('repository').focus();
};
$('toggle-dialogs').onclick = () => {
  const opening = $('dialogs-content').hidden;
  setDialogsOpen(opening);
  if (opening) $('new').focus();
};
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!$('dialogs-content').hidden && $('dialogs-panel').contains(document.activeElement)) {
    event.preventDefault();
    setDialogsOpen(false);
  } else if (!$('context-content').hidden) {
    event.preventDefault();
    setContextOpen(false);
    $('toggle-context').focus();
  } else if (!$('dialogs-content').hidden) {
    event.preventDefault();
    setDialogsOpen(false);
    $('toggle-dialogs').focus();
  }
});
$('context-form').oninput = () => {
  for (const [id, key] of tokenFields) if ($(id).value) tokenRemovals.delete(key);
  dirty = true; $('context-status').textContent = 'There are unsaved changes';
  $('run').disabled = true;
  renderCredentials();
};
for (const [id, key] of tokenFields) {
  $('remove-' + id).onclick = () => {
    $(id).value = ''; tokenRemovals.add(key);
    dirty = true; $('run').disabled = true;
    $('context-status').textContent = 'Token will be removed on Save';
    renderCredentials();
  };
}
$('context-form').onsubmit = event => {
  event.preventDefault();
  guard(async () => {
    if (!state || $('save-context').disabled) return;
    const current = cid;
    const base = structuredClone(contextSpec);
    const fields = Object.fromEntries(contextKeys.map(key => [key, $(key).value.trim() || null]));
    const credentials = {};
    for (const [id, key] of tokenFields) {
      if ($(id).value) credentials[key] = $(id).value;
      else if (tokenRemovals.has(key)) credentials[key] = '';
    }
    $('save-context').disabled = true;
    try {
      const fresh = await api('/conversations/' + current);
      const conflicts = contextKeys.filter(key =>
        (fresh.spec.target[key] ?? null) !== (base.target[key] ?? null)
        && (fresh.spec.target[key] ?? null) !== fields[key]);
      if (conflicts.length) {
        if (cid === current) $('reload-context').hidden = false;
        throw new Error('Project settings changed in another client. Load the latest values or keep a separate copy of your draft.');
      }
      const spec = fresh.spec;
      Object.assign(spec.target, fields);
      await api('/conversations/' + current + '/spec', 'PUT', {spec, expected_version: spec.spec_version, credentials});
      if (cid === current) {
        const tokensUnchanged = tokenFields.every(([id, key]) =>
          ($(id).value || (tokenRemovals.has(key) ? '' : undefined)) === credentials[key]);
        if (tokensUnchanged) {
          for (const [id] of tokenFields) $(id).value = '';
          tokenRemovals.clear();
        }
        const unchanged = tokensUnchanged && contextKeys.every(key => ($(key).value.trim() || null) === fields[key]);
        contextSpec = structuredClone(spec); contextVersion = spec.spec_version + 1;
        dirty = !unchanged;
        $('context-status').textContent = unchanged ? 'Saved' : 'There are new unsaved changes';
        $('reload-context').hidden = true;
        await refresh();
      } else {
        const draft = drafts.get(current);
        if (draft) {
          draft.contextSpec = spec; draft.contextVersion = spec.spec_version + 1;
          draft.dirty = !contextKeys.every(key => (draft.fields[key].trim() || null) === fields[key]);
        }
      }
    } finally { if (cid === current) $('save-context').disabled = false; }
  });
};
$('reload-context').onclick = () => guard(async () => {
  const current = cid;
  const fresh = await api('/conversations/' + current);
  if (current !== cid) return;
  state = fresh; dirty = false; renderContext();
  for (const [id] of tokenFields) $(id).value = '';
  tokenRemovals.clear(); renderCredentials();
  $('reload-context').hidden = true; $('error').hidden = true;
  $('context-status').textContent = 'Latest settings loaded';
});
$('new').onclick = () => guard(async () => open((await api('/conversations', 'POST')).id));
$('run').onclick = () => guard(async () => {
  if (!state || $('run').disabled) return;
  const current = cid, version = state.spec.spec_version;
  $('run').disabled = true;
  try {
    await api('/conversations/' + current + '/runs', 'POST', {
      spec_version: version, request_id: crypto.randomUUID()
    });
  } finally { if (cid === current) await refresh(); }
});
$('cancel').onclick = () => guard(async () => {
  await api('/runs/' + latest().id + '/cancel', 'POST'); await refresh();
});
$('composer').onsubmit = event => {
  event.preventDefault();
  guard(async () => {
    const content = $('input').value.trim(); if (!content) return;
    if (!state || state.busy || pendingMessages.has(cid)) return;
    const current = cid, run = latest();
    const requestKey = JSON.stringify([current, run?.status === 'waiting' ? run.id : '', content]);
    if (!messageRequests.has(requestKey)) messageRequests.set(requestKey, crypto.randomUUID());
    pendingMessages.add(current);
    $('send').disabled = true;
    try {
      if (run?.status === 'waiting') {
        await api('/runs/' + run.id + '/resume', 'POST', {request_id: run.clarification.request_id, answer: content});
      } else {
        await api('/conversations/' + current + '/messages', 'POST', {content, request_id: messageRequests.get(requestKey)});
      }
      messageRequests.delete(requestKey);
      if (cid === current && $('input').value.trim() === content) $('input').value = '';
      const draft = drafts.get(current);
      if (draft?.text.trim() === content) draft.text = '';
      if (cid === current) { await refresh(); await sidebar(); }
    } finally {
      pendingMessages.delete(current);
      if (cid === current) $('send').disabled = !state || state.busy;
    }
  });
};
$('input').onkeydown = event => {
  if (event.ctrlKey && event.key === 'Enter') $('composer').requestSubmit();
};
setContextOpen(localStorage.getItem('perf-chat-context-open') === 'true');
setDialogsOpen(localStorage.getItem('perf-chat-dialogs-open') === 'true');
for (const id of ['repository-settings', 'profiler-settings']) {
  $(id).open = localStorage.getItem('perf-chat-' + id + '-open') !== 'false';
  $(id + '-toggle').onclick = () => localStorage.setItem('perf-chat-' + id + '-open', String(!$(id).open));
  $(id).ontoggle = () => localStorage.setItem('perf-chat-' + id + '-open', String($(id).open));
}
guard(async () => {
  const health = await api('/health');
  const executorName = ({demo: 'demo executor', 'profile-demo': 'synthetic profile analysis',
    'profile-mcp': 'MCP profile analysis'})[health.executor] || 'demo executor';
  $('mode').textContent = (health.mode === 'demo' ? 'Demo mode' : 'Model connected') + ' · ' + executorName;
  const list = await api('/conversations');
  if (!list.some(c => c.id === cid)) cid = list[0]?.id || (await api('/conversations', 'POST')).id;
  await open(cid);
});
