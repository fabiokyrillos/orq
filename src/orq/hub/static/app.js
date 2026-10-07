// orq dashboard: projects, queue, task creation, live runs, summaries and replay. Vanilla ES module, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = document.querySelector('meta[name="orq-token"]').content;
const view = $('#view');
let sources = [];
let projects = [];
let routeKey = '';

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', 'X-Orq-Token': TOKEN };
  const r = await fetch(path, { ...opts, headers });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || r.statusText);
  return data;
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

function closeStreams() { sources.forEach(s => s.close()); sources = []; }

function dur(seconds) {
  if (seconds == null) return '–';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}
function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 60 ? 'just now' : `${dur(s)} ago`;
}
function lines(text) { return String(text || '').split('\n').map(s => s.trim()).filter(Boolean); }
const projectPath = repo => `#/p/${repo}`;

// rail

async function refreshRail() {
  const [list, queue] = await Promise.all([api('/api/projects'), api('/api/queue')]);
  projects = list;
  const current = currentProject();
  $('#projects').innerHTML = `
    <a class="proj ${location.hash.startsWith('#/all') || !location.hash || location.hash === '#/' ? 'active' : ''}" href="#/all">
      <div class="name"><span>All projects</span></div></a>` +
    list.map(p => `
    <a class="proj ${current === p.repo ? 'active' : ''}" href="${projectPath(p.repo)}/runs">
      <div class="name"><span>${esc(p.name)}</span><span class="chips">
        ${p.counts.running ? `<span class="chip running" title="running">${p.counts.running}</span>` : ''}
        ${p.counts.waiting ? `<span class="chip waiting" title="waiting for you">${p.counts.waiting}</span>` : ''}
        ${p.counts.queued ? `<span class="chip queued" title="queued">${p.counts.queued}</span>` : ''}
      </span></div>
      <div class="repo">${esc(p.repo)}</div></a>`).join('');
  $('#slot-text').textContent = `${queue.held}/${queue.limit} · ${queue.queued.length} queued`;
  $('#meter').innerHTML = Array.from({ length: queue.limit }, (_, i) => `<i class="${i < queue.held ? 'on' : queue.queued.length ? 'q' : ''}"></i>`).join('');
}

function currentProject() {
  const m = location.hash.match(/^#\/p\/([^/]+\/[^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

// router

const routes = [
  [/^#\/p\/([^/]+\/[^/]+)\/(runs|queue|new|settings)$/, (m) => projectPage(m[1], m[2])],
  [/^#\/run\/([A-Z0-9]+)(?:\/(live|summary|replay))?$/, (m) => runPage(m[1], m[2] || 'live')],
  [/^#\/add-project$/, () => addProjectPage()],
  [/^#\/settings$/, () => globalSettingsPage()],
  [/^#\/all$/, () => allRuns()],
];

async function route() {
  const hash = location.hash || '#/all';
  closeStreams();
  routeKey = hash;
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try { await fn(m); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
      refreshRail().catch(() => {});
      return;
    }
  }
  location.hash = '#/all';
}
window.addEventListener('hashchange', route);

// runs tables

function runsTable(runs, { showRepo }) {
  if (!runs.length) return '<div class="empty">No runs yet.</div>';
  return `<table><thead><tr><th>run</th><th>state</th><th>task</th>${showRepo ? '<th>project</th>' : ''}<th>progress</th><th>updated</th></tr></thead><tbody>
    ${runs.map(r => `<tr class="click" data-run="${r.run_id}">
      <td class="id">${r.run_id}</td>
      <td><span class="state ${r.state}">${r.state}</span>${r.pending ? '<span class="needs">needs you</span>' : ''}</td>
      <td>${esc(r.task_title)}</td>
      ${showRepo ? `<td class="muted mono small">${esc(r.repo)}</td>` : ''}
      <td class="muted small">iter ${r.iteration}${r.milestone ? ` · m ${r.milestone.index}/${r.milestone.of}` : ''}${r.pr_url ? ' · PR' : ''}</td>
      <td class="muted small">${ago(r.updated_at)}</td></tr>`).join('')}
  </tbody></table>`;
}
function bindRunRows() { $$('tr[data-run]', view).forEach(tr => tr.onclick = () => { location.hash = `#/run/${tr.dataset.run}`; }); }

async function allRuns() {
  const runs = await api('/api/runs');
  view.innerHTML = `<div class="head"><div><h1>All projects</h1><div class="muted small">${runs.length} runs</div></div></div>
    <div class="card">${runsTable(runs, { showRepo: true })}</div>`;
  bindRunRows();
}

// project pages

async function projectPage(repo, tab) {
  await refreshRail();
  const p = projects.find(x => x.repo === repo);
  if (!p) { view.innerHTML = `<div class="empty">No project ${esc(repo)}.</div>`; return; }
  const tabs = [['runs', 'Runs'], ['queue', 'Queue'], ['new', 'New task'], ['settings', 'Settings']]
    .map(([k, label]) => `<a href="${projectPath(repo)}/${k}" class="${k === tab ? 'active' : ''}">${label}</a>`).join('');
  view.innerHTML = `<div class="head"><div><h1>${esc(p.name)}</h1><div class="muted small mono">${esc(p.repo)} · base ${esc(p.base_branch)} · up to ${p.effective_max_concurrent} at once</div></div><nav class="tabs">${tabs}</nav></div><div id="tab"></div>`;
  const el = $('#tab');
  if (tab === 'runs') {
    el.innerHTML = `<div class="card">${runsTable(await api(`/api/runs?project=${encodeURIComponent(repo)}`), { showRepo: false })}</div>`;
    bindRunRows();
  } else if (tab === 'queue') {
    await queueTab(el, repo);
  } else if (tab === 'new') {
    newTaskTab(el, p);
  } else {
    settingsTab(el, p);
  }
}

async function queueTab(el, repo) {
  const q = await api('/api/queue');
  const mine = q.queued.filter(r => r.repo === repo);
  const slots = q.slots.filter(s => s.repo === repo);
  el.innerHTML = `
    <div class="card"><h3 class="muted small">SLOTS ${q.held}/${q.limit} IN USE (ALL PROJECTS)</h3>
      ${slots.length ? `<table><thead><tr><th>run</th><th>status</th><th>since</th></tr></thead><tbody>${slots.map(s => `
        <tr class="click" data-run="${s.run_id}"><td class="id">${s.run_id}</td><td>${s.held ? 'working' : 'waiting for a slot'}</td>
        <td class="muted small">${ago(new Date(s.since * 1000).toISOString())}</td></tr>`).join('')}</tbody></table>`
        : '<div class="muted">No run of this project holds or waits for a slot.</div>'}
    </div>
    <div class="card"><h3 class="muted small">QUEUED, IN ORDER</h3>
      ${mine.length ? `<table><tbody>${mine.map((r, i) => `<tr><td class="muted">${i + 1}</td><td class="id">${r.run_id}</td><td>${esc(r.task_title)}</td>
        <td class="muted small">${ago(r.created_at)}</td><td><button class="danger" data-cancel="${r.run_id}">Cancel</button></td></tr>`).join('')}</tbody></table>`
        : '<div class="muted">Nothing queued.</div>'}
      <div class="hint">The hub starts queued runs when a slot is free. A run waiting for your answer gives its slot back.</div>
      <span class="msg" id="qmsg"></span>
    </div>`;
  bindRunRows();
  $$('[data-cancel]', el).forEach(b => b.onclick = async () => {
    if (!confirm(`Cancel ${b.dataset.cancel}? It will be marked ABORTED.`)) return;
    try { await post(`/api/runs/${b.dataset.cancel}/abort`); route(); }
    catch (e) { $('#qmsg').textContent = e.message; $('#qmsg').className = 'msg err'; }
  });
}

function newTaskTab(el, p) {
  el.innerHTML = `
    <div class="grid2">
      <div class="card">
        <div><button id="mode-form" class="primary">Form</button><button id="mode-md">Paste TASK.md</button></div>
        <form id="task-form">
          <label>Title</label><input name="title" required placeholder="Add a greeting module">
          <label>Goal</label><textarea name="goal" rows="3" placeholder="What and why"></textarea>
          <label>Acceptance criteria</label><textarea name="acceptance_criteria" rows="4" placeholder="One per line"></textarea>
          <label>Out of scope</label><textarea name="out_of_scope" rows="2" placeholder="One per line (optional)"></textarea>
          <label>Constraints</label><textarea name="constraints" rows="2" placeholder="One per line (optional)"></textarea>
          <div class="grid2">
            <div><label>Check command</label><input name="check_command" placeholder="${esc(p.check_command || 'e.g. uv run pytest -q')}"></div>
            <div><label>Base branch</label><input name="base_branch" placeholder="${esc(p.base_branch)}"></div>
          </div>
          <label>Plan approval</label><select name="plan_approval"><option value="required">required: I approve the plan first</option><option value="skip">skip: straight to work</option></select>
          <details style="margin-top:12px"><summary class="muted">Models for this task only (optional)</summary><div id="task-models" class="muted">loading…</div></details>
        </form>
        <div id="md-box" hidden><label>TASK.md</label><textarea id="md" rows="18" placeholder="# Task: ...\n## Repo\n${esc(p.repo)}, base branch ${esc(p.base_branch)}"></textarea></div>
        <div class="errors" id="task-errors"></div>
        <button class="primary" id="queue-task">Queue task</button><span class="msg" id="task-msg"></span>
      </div>
      <div class="card pane"><h3>Preview</h3><pre id="preview" class="muted">Fill in the form.</pre></div>
    </div>`;
  let mode = 'form';
  let aliases = {};
  api('/api/settings').then(payload => {
    aliases = Object.fromEntries(Object.entries(payload.task_aliases).map(([alias, key]) => [key, alias]));
    $('#task-models').innerHTML = settingsForm(payload, {}, p.effective, p.sources, 'task');
    $('#task-models').classList.remove('muted');
    bindModelTests($('#task-models'));
  });
  const taskModels = () => Object.fromEntries(Object.entries(readSettings($('#task-models')))
    .filter(([key, v]) => v !== null && aliases[key]).map(([key, v]) => [aliases[key], String(v)]));
  const body = () => {
    if (mode === 'md') return { project: p.repo, markdown: $('#md').value };
    const f = new FormData($('#task-form'));
    return { project: p.repo, fields: {
      models: taskModels(),
      title: f.get('title'), goal: f.get('goal'), acceptance_criteria: lines(f.get('acceptance_criteria')),
      out_of_scope: lines(f.get('out_of_scope')), constraints: lines(f.get('constraints')),
      check_command: f.get('check_command'), base_branch: f.get('base_branch'), plan_approval: f.get('plan_approval') } };
  };
  let timer = null;
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await post('/api/tasks/preview', body());
        $('#preview').textContent = r.markdown; $('#preview').classList.remove('muted');
        $('#task-errors').textContent = r.errors.join('\n');
      } catch (e) { $('#task-errors').textContent = e.message; }
    }, 300);
  };
  el.addEventListener('input', refresh);
  const setMode = (m) => {
    mode = m;
    $('#task-form').hidden = m !== 'form'; $('#md-box').hidden = m !== 'md';
    $('#mode-form').className = m === 'form' ? 'primary' : ''; $('#mode-md').className = m === 'md' ? 'primary' : '';
    refresh();
  };
  $('#mode-form').onclick = () => setMode('form');
  $('#mode-md').onclick = () => setMode('md');
  $('#queue-task').onclick = async () => {
    $('#queue-task').disabled = true;
    try { const r = await post('/api/tasks', body()); location.hash = `#/run/${r.run_id}`; }
    catch (e) { $('#task-errors').textContent = e.message; }
    finally { $('#queue-task').disabled = false; }
  };
}

function settingsTab(el, p) {
  el.innerHTML = `<div class="card" style="max-width:640px">
    <label>Display name</label><input id="s-name" value="${esc(p.name)}">
    <label>Base branch</label><input id="s-base" value="${esc(p.base_branch)}">
    <label>Default check command</label><input id="s-check" value="${esc(p.check_command)}" placeholder="used when a new task leaves it empty">
    <label>Runs at once for this project</label><input id="s-max" type="number" min="1" value="${p.max_concurrent ?? ''}" placeholder="default ${p.effective_max_concurrent}">
    <div class="hint">Two runs on one repo at once often meet in a rebase conflict, which comes back to you as a decision.</div>
    <div style="margin-top:12px"><button class="primary" id="s-save">Save</button><span class="msg" id="s-msg"></span></div>
    ${p.local_path ? `<div class="hint">Added from ${esc(p.local_path)}. orq works in its own clone, never in that folder.</div>` : ''}
  </div>
  <div class="card" style="max-width:760px" id="ps"><h3 class="muted small">MODELS AND GUARD FOR THIS PROJECT</h3><div class="muted">loading…</div></div>`;
  api('/api/settings').then(payload => {
    $('#ps').innerHTML = `<h3 class="muted small">MODELS AND GUARD FOR THIS PROJECT</h3>${settingsForm(payload, p.settings, p.effective, p.sources, 'project')}
      <div style="margin-top:12px"><button class="primary" id="ps-save">Save</button><span class="msg" id="ps-msg"></span></div>`;
    bindModelTests($('#ps'));
    $('#ps-save').onclick = async () => {
      try {
        await api(`/api/projects/${p.repo}`, { method: 'PATCH', body: JSON.stringify({ settings: readSettings($('#ps')) }) });
        await route(); $('#ps-msg').textContent = 'saved';
      } catch (e) { $('#ps-msg').textContent = e.message; $('#ps-msg').className = 'msg err'; }
    };
  });
  $('#s-save').onclick = async () => {
    const max = $('#s-max').value.trim();
    try {
      await api(`/api/projects/${p.repo}`, { method: 'PATCH', body: JSON.stringify({ name: $('#s-name').value, base_branch: $('#s-base').value,
        check_command: $('#s-check').value, max_concurrent: max ? Number(max) : null }) });
      await route();  // the header shows the new name and cap
      $('#s-msg').textContent = 'saved'; $('#s-msg').className = 'msg';
    } catch (e) { $('#s-msg').textContent = e.message; $('#s-msg').className = 'msg err'; }
  };
}

function addProjectPage() {
  view.innerHTML = `<div class="head"><div><h1>Add project</h1><div class="muted small">A GitHub repo. orq clones it under ~/.orq/repos and never touches your own checkout.</div></div></div>
    <div class="card" style="max-width:640px">
      <label>GitHub repo or local folder</label><input id="p-source" placeholder="owner/repo  or  D:\\Projetos\\my-app">
      <div class="hint">A folder is only used to read its origin remote.</div>
      <label>Display name (optional)</label><input id="p-name">
      <label>Default check command (optional)</label><input id="p-check" placeholder="uv run pytest -q">
      <div style="margin-top:12px"><button class="primary" id="p-add">Add</button><span class="msg" id="p-msg"></span></div>
    </div>`;
  $('#p-add').onclick = async () => {
    $('#p-add').disabled = true; $('#p-msg').textContent = 'checking with gh…'; $('#p-msg').className = 'msg';
    try {
      const p = await post('/api/projects', { source: $('#p-source').value, name: $('#p-name').value || null, check_command: $('#p-check').value || null });
      location.hash = `${projectPath(p.repo)}/new`;
    } catch (e) { $('#p-msg').textContent = e.message; $('#p-msg').className = 'msg err'; }
    finally { $('#p-add').disabled = false; }
  };
}

// settings (Phase 6): one form for the global layer and for a project's layer

const SETTING_LABELS = {
  'implementer.default_model': ['Implementer model', 'Claude model for hard milestones and runs without a plan'],
  'implementer.mechanical_model': ['Implementer model, mechanical', 'Claude model for milestones the planner marks mechanical'],
  'reviewer.codex_model': ['Planner and reviewer model', 'Codex model; changes apply from the next call, even in running runs'],
  'reviewer.claude_model': ['Claude reviewer model', 'when the reviewer is Claude (primary or fallback after a Codex limit)'],
  'reviewer.routine_effort': ['Review effort', 'reasoning effort of the milestone reviews'],
  'reviewer.final_effort': ['Planning and final review effort', 'reasoning effort of the plan and the review before merge'],
  'git.protected_paths': ['Protected paths', 'one glob per line; the guard asks before any change there (applies when a run starts)'],
  'guard.max_net_deleted_lines': ['Max net deleted lines', 'per iteration, before the diff guard asks'],
  'guard.source_globs': ['Source files', 'one glob per line; what counts for the deleted-lines rule'],
};

function settingInput(key, payload, layerValue, inherited) {
  const id = `set-${key.replace(/\./g, '-')}`;
  const shown = layerValue ?? '';
  const ph = Array.isArray(inherited) ? inherited.join('\n') : inherited;
  if (key === 'reviewer.codex_model') {
    const opts = payload.codex_models.map(m => `<option value="${esc(m.slug)}" ${m.slug === shown ? 'selected' : ''}>${esc(m.display_name)} (${esc(m.slug)})</option>`).join('');
    return `<select id="${id}" data-key="${key}"><option value="">inherit: ${esc(ph)}</option>${opts}</select>`;
  }
  if (key.endsWith('_effort')) {
    return `<select id="${id}" data-key="${key}"><option value="">inherit: ${esc(ph)}</option>${payload.efforts.map(e => `<option ${e === shown ? 'selected' : ''}>${e}</option>`).join('')}</select>`;
  }
  if (Array.isArray(inherited)) {
    return `<textarea id="${id}" data-key="${key}" data-list="1" rows="3" placeholder="inherit:\n${esc(ph)}">${esc(Array.isArray(shown) ? shown.join('\n') : '')}</textarea>`;
  }
  if (typeof inherited === 'number') return `<input id="${id}" data-key="${key}" data-int="1" type="number" min="1" value="${esc(shown)}" placeholder="inherit: ${esc(ph)}">`;
  return `<input id="${id}" data-key="${key}" list="claude-models" value="${esc(shown)}" placeholder="inherit: ${esc(ph)}">`;
}

function settingsForm(payload, layer, effective, sources, layerName) {
  const rows = payload.keys.filter(k => layerName === 'task' ? payload.live_keys.includes(k) : true).map(key => {
    const [label, hint] = SETTING_LABELS[key] || [key, ''];
    const src = sources[key];
    const test = key.endsWith('_model') ? `<button type="button" data-test="${key}">Test</button><span class="msg small" data-test-msg="${key}"></span>` : '';
    return `<div class="setting"><label>${esc(label)} <span class="chip">${esc(src === layerName ? 'set here' : 'from ' + src)}</span></label>
      <div style="display:flex;gap:6px;align-items:flex-start">${settingInput(key, payload, layer[key], effective[key])}${test}</div>
      <div class="hint">${esc(hint)} · now: <span class="mono">${esc(Array.isArray(effective[key]) ? effective[key].join(', ') : effective[key])}</span></div></div>`;
  }).join('');
  return `<datalist id="claude-models">${payload.claude_models.map(m => `<option value="${m}">`).join('')}</datalist>${rows}`;
}

function readSettings(el) {
  const out = {};
  $$('[data-key]', el).forEach(input => {
    const v = input.value.trim();
    if (!v) out[input.dataset.key] = null;
    else if (input.dataset.list) out[input.dataset.key] = lines(v);
    else if (input.dataset.int) out[input.dataset.key] = Number(v);
    else out[input.dataset.key] = v;
  });
  return out;
}

function bindModelTests(el) {
  $$('[data-test]', el).forEach(b => b.onclick = async () => {
    const key = b.dataset.test;
    const input = $(`[data-key="${key}"]`, el);
    const model = input.value.trim() || (input.placeholder || '').replace(/^inherit: /, '');
    const kind = key === 'reviewer.codex_model' ? 'codex' : 'claude';
    const msg = $(`[data-test-msg="${key}"]`, el);
    msg.textContent = 'testing…'; msg.className = 'msg small';
    try { const r = await post('/api/models/test', { kind, model }); msg.textContent = r.ok ? `OK (${model})` : r.message; msg.className = `msg small ${r.ok ? '' : 'err'}`; }
    catch (e) { msg.textContent = e.message; msg.className = 'msg small err'; }
  });
}

async function globalSettingsPage() {
  const s = await api('/api/settings');
  view.innerHTML = `<div class="head"><div><h1>Settings</h1><div class="muted small">Defaults for every project. A project or a task can override them. Models and efforts apply from the next agent call, also in running runs.</div></div></div>
    <div class="card" style="max-width:760px" id="gs">${settingsForm(s, s.overrides, s.effective, s.sources, 'global')}
      <div style="margin-top:12px"><button class="primary" id="gs-save">Save</button><span class="msg" id="gs-msg"></span></div></div>`;
  bindModelTests($('#gs'));
  $('#gs-save').onclick = async () => {
    try { await api('/api/settings', { method: 'PUT', body: JSON.stringify(readSettings($('#gs'))) }); await route(); $('#gs-msg').textContent = 'saved'; }
    catch (e) { $('#gs-msg').textContent = e.message; $('#gs-msg').className = 'msg err'; }
  };
}

// run page

async function runPage(id, tab) {
  const d = await api(`/api/runs/${id}`);
  $('#wa').textContent = d.whatsapp ? 'WhatsApp on' : 'WhatsApp off (dashboard only)';
  const finished = ['DONE', 'FAILED', 'ABORTED'].includes(d.state);
  const tabs = [['live', 'Live'], ['summary', 'Summary'], ['replay', 'Replay']]
    .map(([k, label]) => `<a href="#/run/${id}/${k}" class="${k === tab ? 'active' : ''}">${label}</a>`).join('');
  const plan = d.plan && d.plan.milestones ? `<div class="muted small">plan: ${d.plan.milestones.map((m, i) => `${i + 1}. ${esc(m.title)} [${m.difficulty}]`).join(' · ')}</div>` : '';
  const decisions = d.decisions.map(x => `
    <div class="card decision ${x.destructive ? 'destructive' : ''}" data-decision="${x.decision_id}">
      <div><b class="mono">${x.decision_id}</b> <span class="muted">${esc(x.source)} · ${esc(x.decision_type)}${x.destructive ? ' · destructive' : ''}</span></div>
      ${x.context ? `<div class="ctx">${esc(x.context)}</div>` : ''}
      <div class="q"><b>${esc(x.question)}</b></div>
      ${x.option_details && x.option_details.length ? `<ol class="opts" start="0">${x.options.map((o, i) => `<li><b>${esc(o)}</b>${x.option_details[i] ? `: ${esc(x.option_details[i])}` : ''}</li>`).join('')}</ol>` : ''}
      ${x.recommendation_reason && x.recommendation != null ? `<div class="hint">Recommended: ${esc(x.options[x.recommendation] ?? '')}, because ${esc(x.recommendation_reason)}</div>` : ''}
      <div>${x.options.map((o, i) => `<button data-answer="${esc(o)}" class="${x.destructive && o === 'deny' ? 'danger' : ''}">${i}. ${esc(o)}${x.recommendation === i ? ' ★' : ''}</button>`).join('')}</div>
      <div style="margin-top:8px;display:flex;gap:6px"><input type="text" placeholder="free text answer"><button data-send>Send</button></div>
    </div>`).join('');
  view.innerHTML = `
    <div class="head"><div>
      <h1><span class="id">${d.run_id}</span> <span class="state ${d.state}">${d.state}</span> ${esc(d.task_title)}</h1>
      <div class="muted small"><a href="${projectPath(d.repo)}/runs" class="mono">${esc(d.repo)}</a> · ${esc(d.branch)} · iteration ${d.iteration} · phase ${esc(d.phase)}${d.pr_url ? ` · <a href="${d.pr_url}" target="_blank">PR</a>` : ''}</div>
      ${plan}
      <div style="margin:8px 0">
        ${finished ? '' : '<button data-act="pause">Pause</button><button data-act="resume">Resume</button><button data-act="abort" class="danger">Abort</button>'}
        <button data-act="rerun">Run again</button><span class="msg" id="msg"></span>
      </div></div><nav class="tabs">${tabs}</nav></div>
    ${decisions}<div id="tab"></div>`;
  $$('[data-act]').forEach(b => b.onclick = () => act(id, b.dataset.act));
  $$('.decision').forEach(card => {
    const did = card.dataset.decision;
    $$('[data-answer]', card).forEach(b => b.onclick = () => answer(id, did, b.dataset.answer));
    $('[data-send]', card).onclick = () => answer(id, did, $('input', card).value);
  });
  const el = $('#tab');
  if (tab === 'live') liveTab(el, id);
  else if (tab === 'summary') await summaryTab(el, id);
  else await replayTab(el, id);
}

function attach(url, pre) {
  const es = new EventSource(url);
  es.onmessage = e => { pre.textContent += e.data + '\n'; pre.scrollTop = pre.scrollHeight; };
  es.addEventListener('end', () => es.close());
  es.onerror = () => {};
  sources.push(es);
}

function liveTab(el, id) {
  el.innerHTML = `
    <div class="pane card"><h3>events</h3><pre id="events"></pre></div>
    <div class="grid2">
      <div class="pane card"><h3>implementer (claude)</h3><pre id="impl"></pre></div>
      <div class="pane card"><h3>reviewer</h3><pre id="rev"></pre></div>
    </div>`;
  attach(`/api/runs/${id}/events`, $('#events'));
  attach(`/api/runs/${id}/stream/implementer`, $('#impl'));
  attach(`/api/runs/${id}/stream/reviewer`, $('#rev'));
}

async function summaryTab(el, id) {
  const s = await api(`/api/runs/${id}/summary`);
  const totalState = Object.values(s.time_by_state).reduce((a, b) => a + b, 0) || 1;
  const agentRows = Object.entries(s.agents).filter(([, a]) => a.calls).map(([role, a]) => `<tr><td>${role}</td><td>${a.calls}${a.failed ? ` <span class="muted">(${a.failed} failed)</span>` : ''}</td>
    <td class="mono">${dur(a.seconds)}</td><td class="mono">${a.input_tokens.toLocaleString()}</td><td class="mono">${a.output_tokens.toLocaleString()}</td></tr>`).join('');
  el.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${dur(s.wall_seconds)}</b><span>wall time</span></div>
      <div class="kpi"><b>${s.iterations}</b><span>iterations</span></div>
      <div class="kpi"><b>${s.milestones.done}/${s.milestones.planned}</b><span>milestones</span></div>
      <div class="kpi"><b>${s.decisions.length}</b><span>decisions</span></div>
      <div class="kpi"><b>${s.checks.runs - s.checks.failed}/${s.checks.runs}</b><span>checks passed</span></div>
      <div class="kpi"><b>${s.pr.merged ? 'merged' : s.pr.url ? 'open' : '–'}</b><span>pull request</span></div>
    </div>
    <div class="grid2">
      <div class="card"><h3 class="muted small">TIME BY STATE</h3><div class="bars">
        ${Object.entries(s.time_by_state).sort((a, b) => b[1] - a[1]).map(([state, sec]) => `<span class="mono small">${state}</span>
          <div class="track"><div class="fill ${state}" style="width:${Math.max(1, 100 * sec / totalState)}%"></div></div><span class="v">${dur(sec)}</span>`).join('')}
      </div></div>
      <div class="card"><h3 class="muted small">AGENTS</h3>
        <table><thead><tr><th>role</th><th>calls</th><th>time</th><th>input tok</th><th>output tok</th></tr></thead><tbody>${agentRows || '<tr><td colspan="5" class="muted">none</td></tr>'}</tbody></table>
        <div class="hint">models: ${esc([...new Set(s.implementer_models)].join(', ') || '–')} · reviewer switches: ${s.reviewer_switches}
        ${s.ci ? ` · CI ${esc(s.ci.state)} after ${dur(s.ci.waited_seconds)}${s.ci.reruns ? `, ${s.ci.reruns} re-run(s)` : ''}` : ''}
        · guard: ${s.guard.pre_denials} denial(s), ${s.guard.diff_rule_hits} diff-rule hit(s)</div>
      </div>
    </div>
    <div class="card"><h3 class="muted small">DECISIONS</h3>
      ${s.decisions.length ? `<table><thead><tr><th>id</th><th>from</th><th>question</th><th>answer</th><th>via</th><th>waited</th></tr></thead><tbody>
        ${s.decisions.map(x => `<tr><td class="mono">${x.decision_id}</td><td class="muted">${esc(x.source)}</td><td>${esc(x.question)}</td>
        <td>${esc(x.answer ?? 'pending')}</td><td class="muted">${esc(x.via ?? '')}</td><td class="mono">${dur(x.waited_seconds)}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">None. The run needed no input from you.</div>'}
    </div>
    <div class="card"><h3 class="muted small">FILES CHANGED</h3><div class="mono small">${s.files_changed.map(esc).join('<br>') || '<span class="muted">none</span>'}</div></div>`;
}

function renderArtifact(path, text) {
  if (path.endsWith('.patch')) {
    return `<pre class="diff">${text.split('\n').map(l => {
      const cls = l.startsWith('+') && !l.startsWith('+++') ? 'add' : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : '';
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join('\n')}</pre>`;
  }
  if (path.endsWith('.json')) { try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ } }
  return `<pre>${esc(text)}</pre>`;
}

async function replayTab(el, id) {
  const replay = await api(`/api/runs/${id}/replay`);
  const steps = replay.steps;
  el.innerHTML = `<div class="replay">
      <div class="card steps" style="padding:0">${steps.map((s, i) => `<a href="javascript:void 0" data-step="${i}" class="${s.discarded ? 'discarded' : ''}">${esc(s.label)}
        <div class="muted small">${s.events.length} events${s.events[0] ? ' · ' + new Date(s.events[0].ts).toLocaleTimeString() : ''}</div></a>`).join('')}</div>
      <div><div class="card"><div class="artifacts" id="arts"></div><div id="art"></div></div>
        <div class="card"><h3 class="muted small">EVENTS</h3><div id="evts"></div></div></div>
    </div>`;
  const show = async (i) => {
    $$('[data-step]', el).forEach(a => a.classList.toggle('active', Number(a.dataset.step) === i));
    const step = steps[i];
    $('#evts').innerHTML = step.events.map(e => {
      const { ts, type, ...rest } = e;
      const detail = Object.entries(rest).filter(([k]) => !['usage', 'rate_limit', 'session_id'].includes(k))
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
      return `<div class="evt"><span class="t">${new Date(ts).toLocaleTimeString()}</span><b>${esc(type)}</b> ${esc(detail.slice(0, 400))}</div>`;
    }).join('') || '<div class="muted">no events</div>';
    $('#arts').innerHTML = step.artifacts.map(a => `<button data-art="${esc(a)}">${esc(a.split('/').pop())}</button>`).join('') || '<span class="muted">no files recorded</span>';
    $('#art').innerHTML = '';
    const open = async (path) => {
      $$('[data-art]', el).forEach(b => b.classList.toggle('active', b.dataset.art === path));
      const r = await api(`/api/runs/${id}/artifact?path=${encodeURIComponent(path)}`);
      $('#art').innerHTML = (r.truncated ? '<div class="hint">showing the end of a long file</div>' : '') + renderArtifact(path, r.text);
    };
    $$('[data-art]', el).forEach(b => b.onclick = () => open(b.dataset.art));
    const first = step.artifacts.find(a => a.endsWith('diff.patch')) || step.artifacts[0];
    if (first) open(first);
  };
  $$('[data-step]', el).forEach(a => a.onclick = () => show(Number(a.dataset.step)));
  if (steps.length) show(steps.length > 1 ? 1 : 0);
}

async function answer(runId, decisionId, text) {
  if (!text) return;
  try { await post(`/api/decisions/${decisionId}/answer`, { answer: text }); $('#msg').textContent = `${decisionId} answered: ${text}`; }
  catch (e) { $('#msg').textContent = e.message; $('#msg').className = 'msg err'; }
  setTimeout(route, 500);
}

async function act(runId, what) {
  if (what === 'abort' && !confirm(`Abort ${runId}? Its worktree is removed.`)) return;
  try {
    const r = await post(`/api/runs/${runId}/${what}`);
    if (what === 'rerun') { location.hash = `#/run/${r.run_id}`; return; }
    $('#msg').textContent = `${what}: ok${r.pid ? ' (pid ' + r.pid + ')' : ''}${r.warning ? ' · ' + r.warning : ''}`;
  } catch (e) { $('#msg').textContent = e.message; $('#msg').className = 'msg err'; }
  setTimeout(route, 800);
}

// periodic refresh: the rail always; list views when nothing is being typed

setInterval(() => {
  $('#clock').textContent = new Date().toLocaleTimeString();
  refreshRail().catch(() => {});
  const typing = document.activeElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
  if (!typing && /^#\/(all|p\/[^/]+\/[^/]+\/(runs|queue))$/.test(location.hash) && routeKey === location.hash) route();
}, 5000);

route();
