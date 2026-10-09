// orq dashboard: projects, queue, task creation, live runs, summaries, replay, usage and chat. Vanilla ES module, no build step.
// Texts the owner reads are in Brazilian Portuguese (CLAUDE.md exception); machine keywords and IDs stay as they are.
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
  return s < 60 ? 'agora' : `há ${dur(s)}`;
}
const num = n => Number(n || 0).toLocaleString('pt-BR');
function lines(text) { return String(text || '').split('\n').map(s => s.trim()).filter(Boolean); }
const projectPath = repo => `#/p/${repo}`;

const STATE_LABELS = {
  QUEUED: 'na fila', PLANNING: 'planejando', AWAITING_PLAN_APPROVAL: 'aprovar plano', IMPLEMENTING: 'implementando',
  VERIFYING: 'verificando', REVIEWING: 'revisando', FINALIZING: 'finalizando', DONE: 'concluído', AWAITING_HUMAN: 'esperando você',
  PAUSED_RATE_LIMIT: 'pausado (limite)', PAUSED: 'pausado', FAILED: 'falhou', ABORTED: 'abortado',
};
const stateChip = s => `<span class="state ${s}" title="${esc(s)}">${esc(STATE_LABELS[s] || s)}</span>`;
const ROLE_LABELS = { planner: 'planejador', implementer: 'implementador', reviewer: 'revisor', chat: 'conversa' };
const BY_LABELS = { owner: 'você', claude: 'Claude', auto: 'orq (automático)' };

// rail

function railItem(p, current) {
  return `<a class="proj ${current === p.repo ? 'active' : ''}" href="${projectPath(p.repo)}/runs">
      <div class="name"><span>${p.pinned ? '<span class="star" title="fixado">★</span> ' : ''}${esc(p.name)}</span><span class="chips">
        ${p.counts.running ? `<span class="chip running" title="rodando">${p.counts.running}</span>` : ''}
        ${p.counts.waiting ? `<span class="chip waiting" title="esperando você">${p.counts.waiting}</span>` : ''}
        ${p.counts.queued ? `<span class="chip queued" title="na fila">${p.counts.queued}</span>` : ''}
      </span></div>
      <div class="repo">${esc(p.repo)}</div></a>`;
}

async function refreshRail() {
  const [list, queue] = await Promise.all([api('/api/projects'), api('/api/queue')]);
  projects = list;
  const current = currentProject();
  const byPin = (a, b) => (b.pinned - a.pinned) || a.name.localeCompare(b.name, 'pt-BR');
  const active = list.filter(p => p.status === 'active').sort(byPin);
  const archived = list.filter(p => p.status === 'archived').sort(byPin);
  const openArchived = archived.some(p => p.repo === current) ? 'open' : '';
  $('#projects').innerHTML = `
    <a class="proj ${location.hash.startsWith('#/all') || !location.hash || location.hash === '#/' ? 'active' : ''}" href="#/all">
      <div class="name"><span>Todos os runs</span></div></a>
    <a class="proj ${location.hash.startsWith('#/usage') ? 'active' : ''}" href="#/usage"><div class="name"><span>Uso</span></div></a>
    <div class="rail-label">projetos</div>` +
    (active.map(p => railItem(p, current)).join('') || '<div class="muted small rail-empty">Nenhum projeto ativo.</div>') +
    (archived.length ? `<details class="archived" ${openArchived}><summary>Arquivados (${archived.length})</summary>${archived.map(p => railItem(p, current)).join('')}</details>` : '');
  $('#slot-text').textContent = `${queue.held}/${queue.limit} · ${queue.queued.length} na fila`;
  $('#meter').innerHTML = Array.from({ length: queue.limit }, (_, i) => `<i class="${i < queue.held ? 'on' : queue.queued.length ? 'q' : ''}"></i>`).join('');
}

function currentProject() {
  const m = location.hash.match(/^#\/p\/([^/]+\/[^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

// router

const routes = [
  [/^#\/p\/([^/]+\/[^/]+)\/(runs|queue|new|chat|usage|settings)(?:\/([A-Za-z0-9]+))?$/, (m) => projectPage(m[1], m[2], m[3])],
  [/^#\/run\/([A-Z0-9]+)(?:\/(live|summary|replay))?$/, (m) => runPage(m[1], m[2] || 'live')],
  [/^#\/add-project$/, () => addProjectPage()],
  [/^#\/settings$/, () => globalSettingsPage()],
  [/^#\/usage$/, () => usagePage()],
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
  if (!runs.length) return '<div class="empty">Nenhum run ainda.</div>';
  return `<table><thead><tr><th>run</th><th>estado</th><th>task</th>${showRepo ? '<th>projeto</th>' : ''}<th>progresso</th><th>atualizado</th></tr></thead><tbody>
    ${runs.map(r => `<tr class="click" data-run="${r.run_id}">
      <td class="id">${r.run_id}</td>
      <td>${stateChip(r.state)}${r.pending ? '<span class="needs">precisa de você</span>' : ''}</td>
      <td>${esc(r.task_title)}</td>
      ${showRepo ? `<td class="muted mono small">${esc(r.repo)}</td>` : ''}
      <td class="muted small">iteração ${r.iteration}${r.milestone ? ` · etapa ${r.milestone.index}/${r.milestone.of}` : ''}${r.pr_url ? ' · PR' : ''}</td>
      <td class="muted small">${ago(r.updated_at)}</td></tr>`).join('')}
  </tbody></table>`;
}
function bindRunRows() { $$('tr[data-run]', view).forEach(tr => tr.onclick = () => { location.hash = `#/run/${tr.dataset.run}`; }); }

async function allRuns() {
  const runs = await api('/api/runs');
  view.innerHTML = `<div class="head"><div><h1>Todos os runs</h1><div class="muted small">${runs.length} runs, de todos os projetos (inclusive arquivados e removidos)</div></div></div>
    <div class="card">${runsTable(runs, { showRepo: true })}</div>`;
  bindRunRows();
}

// project pages

async function projectPage(repo, tab, sub) {
  await refreshRail();
  let p = projects.find(x => x.repo === repo);
  if (!p) {
    const all = await api('/api/projects?all=1');
    p = all.find(x => x.repo === repo);
    if (!p) { view.innerHTML = `<div class="empty">Projeto ${esc(repo)} não encontrado.</div>`; return; }
  }
  const tabs = [['runs', 'Runs'], ['queue', 'Fila'], ['new', 'Nova task'], ['chat', 'Conversa'], ['usage', 'Uso'], ['settings', 'Configurações']]
    .map(([k, label]) => `<a href="${projectPath(repo)}/${k}" class="${k === tab ? 'active' : ''}">${label}</a>`).join('');
  const banner = p.status === 'archived'
    ? `<div class="banner">Projeto arquivado: não recebe novas tasks. <button id="p-restore">Restaurar</button></div>`
    : p.status === 'removed' ? '<div class="banner">Projeto removido. Adicione de novo em "Adicionar projeto" para voltar a usar.</div>' : '';
  view.innerHTML = `<div class="head"><div>
      <h1><button class="pin ${p.pinned ? 'on' : ''}" id="p-pin" title="${p.pinned ? 'desafixar' : 'fixar no topo'}">${p.pinned ? '★' : '☆'}</button> ${esc(p.name)}</h1>
      <div class="muted small mono">${esc(p.repo)} · base ${esc(p.base_branch)} · até ${p.effective_max_concurrent} ao mesmo tempo</div></div>
    <nav class="tabs">${tabs}</nav></div>${banner}<div id="tab"></div>`;
  $('#p-pin').onclick = async () => { await post(`/api/projects/${p.repo}/pin`, { pinned: !p.pinned }); route(); };
  if ($('#p-restore')) $('#p-restore').onclick = async () => { await post(`/api/projects/${p.repo}/restore`); route(); };
  const el = $('#tab');
  if (tab === 'runs') {
    el.innerHTML = `<div class="card">${runsTable(await api(`/api/runs?project=${encodeURIComponent(repo)}`), { showRepo: false })}</div>`;
    bindRunRows();
  } else if (tab === 'queue') {
    await queueTab(el, repo);
  } else if (tab === 'new') {
    if (p.status !== 'active') el.innerHTML = '<div class="empty">Restaure o projeto para criar tasks.</div>';
    else newTaskTab(el, p);
  } else if (tab === 'chat') {
    await chatTab(el, p, sub);
  } else if (tab === 'usage') {
    await usageView(el, p.repo);
  } else {
    settingsTab(el, p);
  }
}

async function queueTab(el, repo) {
  const q = await api('/api/queue');
  const mine = q.queued.filter(r => r.repo === repo);
  const slots = q.slots.filter(s => s.repo === repo);
  el.innerHTML = `
    <div class="card"><h3 class="muted small">SLOTS ${q.held}/${q.limit} EM USO (TODOS OS PROJETOS)</h3>
      ${slots.length ? `<table><thead><tr><th>run</th><th>situação</th><th>desde</th></tr></thead><tbody>${slots.map(s => `
        <tr class="click" data-run="${s.run_id}"><td class="id">${s.run_id}</td><td>${s.held ? 'trabalhando' : 'esperando um slot'}</td>
        <td class="muted small">${ago(new Date(s.since * 1000).toISOString())}</td></tr>`).join('')}</tbody></table>`
        : '<div class="muted">Nenhum run deste projeto ocupa ou espera um slot.</div>'}
    </div>
    <div class="card"><h3 class="muted small">NA FILA, EM ORDEM</h3>
      ${mine.length ? `<table><tbody>${mine.map((r, i) => `<tr><td class="muted">${i + 1}</td><td class="id">${r.run_id}</td><td>${esc(r.task_title)}</td>
        <td class="muted small">${ago(r.created_at)}</td>
        <td style="white-space:nowrap"><button data-move="top" data-run-id="${r.run_id}" title="primeiro">⤒</button><button data-move="up" data-run-id="${r.run_id}" title="subir">↑</button><button data-move="down" data-run-id="${r.run_id}" title="descer">↓</button>
          <button data-edit="${r.run_id}">Editar</button><button class="danger" data-cancel="${r.run_id}">Cancelar</button></td></tr>`).join('')}</tbody></table>`
        : '<div class="muted">Nada na fila.</div>'}
      <div class="hint">O hub inicia os runs da fila nesta ordem quando um slot fica livre (os slots são compartilhados entre os projetos). Um run esperando sua resposta devolve o slot.</div>
      <span class="msg" id="qmsg"></span>
    </div>
    <div class="card" id="qedit" hidden><h3 class="muted small">EDITAR <span id="qedit-id" class="mono"></span></h3>
      <textarea id="qedit-md" rows="18"></textarea><div class="errors" id="qedit-err"></div>
      <button class="primary" id="qedit-save">Salvar</button><button id="qedit-close">Fechar</button></div>`;
  const qmsg = (text, err) => { $('#qmsg').textContent = text; $('#qmsg').className = `msg ${err ? 'err' : ''}`; };
  $$('[data-move]', el).forEach(b => b.onclick = async () => {
    try { await post(`/api/runs/${b.dataset.runId}/move`, { to: b.dataset.move }); route(); } catch (e) { qmsg(e.message, true); }
  });
  $$('[data-edit]', el).forEach(b => b.onclick = async () => {
    const id = b.dataset.edit;
    try {
      const r = await api(`/api/runs/${id}/artifact?path=TASK.md`);
      $('#qedit').hidden = false; $('#qedit-id').textContent = id; $('#qedit-md').value = r.text; $('#qedit-err').textContent = '';
      $('#qedit-save').onclick = async () => {
        try { await api(`/api/runs/${id}/task`, { method: 'PUT', body: JSON.stringify({ markdown: $('#qedit-md').value }) }); route(); }
        catch (e) { $('#qedit-err').textContent = e.message; }
      };
      $('#qedit-close').onclick = () => { $('#qedit').hidden = true; };
    } catch (e) { qmsg(e.message, true); }
  });
  bindRunRows();
  $$('[data-cancel]', el).forEach(b => b.onclick = async () => {
    if (!confirm(`Cancelar ${b.dataset.cancel}? Ele fica marcado como ABORTED.`)) return;
    try { await post(`/api/runs/${b.dataset.cancel}/abort`); route(); }
    catch (e) { qmsg(e.message, true); }
  });
}

function newTaskTab(el, p) {
  el.innerHTML = `
    <div class="grid2">
      <div class="card">
        <div><button id="mode-form" class="primary">Formulário</button><button id="mode-md">Colar TASK.md</button></div>
        <form id="task-form">
          <label>Título</label><input name="title" required placeholder="Add a greeting module">
          <label>Objetivo</label><textarea name="goal" rows="3" placeholder="O que e por quê"></textarea>
          <label>Critérios de aceite</label><textarea name="acceptance_criteria" rows="4" placeholder="Um por linha"></textarea>
          <label>Fora do escopo</label><textarea name="out_of_scope" rows="2" placeholder="Um por linha (opcional)"></textarea>
          <label>Restrições</label><textarea name="constraints" rows="2" placeholder="Uma por linha (opcional)"></textarea>
          <div class="grid2">
            <div><label>Comando de checagem</label><input name="check_command" placeholder="${esc(p.check_command || 'ex.: uv run pytest -q')}"></div>
            <div><label>Branch base</label><input name="base_branch" placeholder="${esc(p.base_branch)}"></div>
          </div>
          <label>Aprovação do plano</label><select name="plan_approval"><option value="required">obrigatória: eu aprovo o plano antes</option><option value="skip">pular: direto para o trabalho</option></select>
          <details style="margin-top:12px"><summary class="muted">Modelos só para esta task (opcional)</summary><div id="task-models" class="muted">carregando…</div></details>
        </form>
        <div id="md-box" hidden><label>TASK.md</label><textarea id="md" rows="18" placeholder="# Task: ...\n## Repo\n${esc(p.repo)}, base branch ${esc(p.base_branch)}"></textarea></div>
        <div class="errors" id="task-errors"></div>
        <button class="primary" id="queue-task">Colocar na fila</button><span class="msg" id="task-msg"></span>
      </div>
      <div class="card pane"><h3>Prévia</h3><pre id="preview" class="muted">Preencha o formulário.</pre></div>
    </div>
    <div class="hint">O TASK.md é lido pelos agentes; os títulos das seções (## Goal etc.) ficam em inglês.</div>`;
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
  const archived = p.status === 'archived';
  el.innerHTML = `<div class="card" style="max-width:640px">
    <label>Nome de exibição</label><input id="s-name" value="${esc(p.name)}">
    <label>Branch base</label><input id="s-base" value="${esc(p.base_branch)}">
    <label>Comando de checagem padrão</label><input id="s-check" value="${esc(p.check_command)}" placeholder="usado quando uma task nova deixa vazio">
    <label>Runs ao mesmo tempo neste projeto</label><input id="s-max" type="number" min="1" value="${p.max_concurrent ?? ''}" placeholder="padrão ${p.effective_max_concurrent}">
    <div class="hint">Dois runs no mesmo repositório ao mesmo tempo costumam se encontrar num conflito de rebase, que volta para você como decisão.</div>
    <div style="margin-top:12px"><button class="primary" id="s-save">Salvar</button><span class="msg" id="s-msg"></span></div>
    ${p.local_path ? `<div class="hint">O clone do orq foi feito a partir de ${esc(p.local_path)}. O orq só lê essa pasta e trabalha no próprio clone.</div>` : ''}
  </div>
  <div class="card" style="max-width:760px" id="ps"><h3 class="muted small">MODELOS E GUARD DESTE PROJETO</h3><div class="muted">carregando…</div></div>
  <div class="card" style="max-width:760px"><h3 class="muted small">ARQUIVAR OU REMOVER</h3>
    <div class="hint">Arquivar esconde o projeto num grupo "Arquivados" e guarda tudo (clone, configurações); restaurar volta como estava.
      Remover apaga o clone do orq e as worktrees dos runs deste projeto; o histórico de runs continua em "Todos os runs" e a sua pasta nunca é tocada.
      Os dois são recusados enquanto houver run ativo, esperando você ou na fila.</div>
    <div style="margin-top:10px">${archived ? '<button id="s-restore">Restaurar</button>' : '<button id="s-archive">Arquivar</button>'}<button class="danger" id="s-remove">Remover</button><span class="msg" id="s-life"></span></div>
  </div>`;
  api('/api/settings').then(payload => {
    $('#ps').innerHTML = `<h3 class="muted small">MODELOS E GUARD DESTE PROJETO</h3>${settingsForm(payload, p.settings, p.effective, p.sources, 'project')}
      <div style="margin-top:12px"><button class="primary" id="ps-save">Salvar</button><span class="msg" id="ps-msg"></span></div>`;
    bindSections($('#ps'));
    bindModelTests($('#ps'));
    $('#ps-save').onclick = async () => {
      try {
        await api(`/api/projects/${p.repo}`, { method: 'PATCH', body: JSON.stringify({ settings: readSettings($('#ps')) }) });
        await route(); $('#ps-msg').textContent = 'salvo';
      } catch (e) { $('#ps-msg').textContent = e.message; $('#ps-msg').className = 'msg err'; }
    };
  });
  $('#s-save').onclick = async () => {
    const max = $('#s-max').value.trim();
    try {
      await api(`/api/projects/${p.repo}`, { method: 'PATCH', body: JSON.stringify({ name: $('#s-name').value, base_branch: $('#s-base').value,
        check_command: $('#s-check').value, max_concurrent: max ? Number(max) : null }) });
      await route();  // the header shows the new name and cap
      $('#s-msg').textContent = 'salvo'; $('#s-msg').className = 'msg';
    } catch (e) { $('#s-msg').textContent = e.message; $('#s-msg').className = 'msg err'; }
  };
  const life = async (what, path, method = 'POST') => {
    try { await api(path, { method }); if (what === 'remove') location.hash = '#/all'; else await route(); }
    catch (e) { $('#s-life').textContent = e.message; $('#s-life').className = 'msg err'; }
  };
  if ($('#s-archive')) $('#s-archive').onclick = () => life('archive', `/api/projects/${p.repo}/archive`);
  if ($('#s-restore')) $('#s-restore').onclick = () => life('restore', `/api/projects/${p.repo}/restore`);
  $('#s-remove').onclick = () => {
    if (!confirm(`Remover ${p.repo}? O clone do orq e as worktrees dos runs são apagados. Sua pasta e o histórico de runs ficam.`)) return;
    life('remove', `/api/projects/${p.repo}`, 'DELETE');
  };
}

async function addProjectPage() {
  view.innerHTML = `<div class="head"><div><h1>Adicionar projeto</h1><div class="muted small">Escolha um repositório que já está neste PC, ou um que só existe no GitHub. O orq trabalha no próprio clone em ~/.orq/repos; uma pasta sua só é lida, para servir de fonte desse clone.</div></div></div>
    <div class="card" style="max-width:900px"><h3 class="muted small">PADRÕES PARA O QUE VOCÊ ADICIONAR</h3>
      <label>Nome de exibição (opcional, só ao adicionar um)</label><input id="p-name">
      <label>Comando de checagem padrão (opcional)</label><input id="p-check" placeholder="uv run pytest -q">
      <div style="margin-top:12px"><button class="primary" id="p-bulk" disabled>Adicionar selecionados</button><span class="msg" id="p-msg"></span></div>
      <div id="p-results" class="small"></div></div>
    <div class="card" style="max-width:900px" id="p-local"><h3 class="muted small">NESTE PC</h3><div class="muted">olhando suas pastas…</div></div>
    <div class="card" style="max-width:900px" id="p-github"><h3 class="muted small">SÓ NO GITHUB</h3><div class="muted">consultando o gh…</div></div>
    <div class="card" style="max-width:900px"><h3 class="muted small">OUTRO</h3>
      <label>Repositório do GitHub ou pasta local</label><input id="p-source" placeholder="owner/repo  ou  D:\\Projetos\\meu-app">
      <div style="margin-top:12px"><button class="primary" id="p-add">Adicionar</button></div></div>`;
  const msg = (text, err) => { $('#p-msg').textContent = text; $('#p-msg').className = `msg ${err ? 'err' : ''}`; };
  const addOne = (source, name) => post('/api/projects', { source, name: name || null, check_command: $('#p-check').value || null });
  const add = async (source, button) => {
    if (button) button.disabled = true;
    msg('verificando com o gh…');
    try { const p = await addOne(source, $('#p-name').value); location.hash = `${projectPath(p.repo)}/new`; }
    catch (e) { msg(e.message, true); if (button) button.disabled = false; }
  };
  $('#p-add').onclick = () => add($('#p-source').value.trim(), $('#p-add'));
  const marks = r => `${r.private ? '<span class="chip" title="no GitHub Actions, repositório privado gasta os minutos do seu plano">privado</span> ' : ''}${r.added ? '<span class="chip">adicionado</span>' : ''}`;
  let found;
  try { found = await api('/api/projects/candidates'); }
  catch (e) { $('#p-local').innerHTML = `<h3 class="muted small">NESTE PC</h3><div class="msg err">${esc(e.message)}</div>`; $('#p-github').innerHTML = ''; return; }
  const box = (kind, i, r) => r.added ? '' : `<input type="checkbox" class="pick" data-kind="${kind}" data-i="${i}">`;
  $('#p-local').innerHTML = `<h3 class="muted small">NESTE PC</h3>
    ${!found.roots.length ? '<div class="hint">Ainda não há pastas para procurar. Configure "Pastas com seus repositórios" em <a href="#/settings">Configurações</a>.</div>'
      : !found.local.length ? `<div class="muted">Nenhum checkout com origin no GitHub em ${esc(found.roots.join(', '))}.</div>`
      : `<table><thead><tr><th></th><th>repositório</th><th>pasta</th><th>seu trabalho lá</th><th></th></tr></thead><tbody>${found.local.map((r, i) => `<tr>
          <td>${box('local', i, r)}</td><td>${esc(r.repo)} ${marks(r)}</td><td class="mono small">${esc(r.path)}</td>
          <td class="small">${esc(r.branch || 'detached')}${r.dirty ? `, ${r.dirty} sem commit` : ''}</td>
          <td><button data-local="${i}" ${r.added ? 'title="atualiza o projeto: a pasta dele passa a ser esta"' : ''}>${r.added ? 'Usar esta pasta' : 'Adicionar'}</button></td></tr>`).join('')}</tbody></table>
        <div class="hint">O orq nunca trabalha nessas pastas: lê uma delas para montar o próprio clone e depois busca do GitHub. Sua branch e suas mudanças sem commit ficam como estão.</div>`}`;
  $$('[data-local]').forEach(b => b.onclick = () => add(found.local[Number(b.dataset.local)].path, b));
  const github = found.github;
  const rows = filter => github.map((r, i) => [r, i]).filter(([r]) => !filter || r.repo.toLowerCase().includes(filter) || r.description.toLowerCase().includes(filter))
    .map(([r, i]) => `<tr><td>${box('github', i, r)}</td><td>${esc(r.repo)} ${marks(r)}</td><td class="small muted">${esc(r.description)}</td><td class="small muted">${esc((r.pushed_at || '').slice(0, 10))}</td>
      <td>${r.added ? '' : `<button data-github="${i}">Adicionar</button>`}</td></tr>`).join('');
  $('#p-github').innerHTML = `<h3 class="muted small">SÓ NO GITHUB</h3>
    ${found.github_error ? `<div class="msg err">o gh falhou: ${esc(found.github_error)}</div>` : `
      <input id="p-filter" placeholder="filtrar">
      <table><thead><tr><th></th><th>repositório</th><th>descrição</th><th>último push</th><th></th></tr></thead><tbody id="p-github-rows">${rows('')}</tbody></table>
      <div class="hint">O orq clona em ~/.orq/repos no primeiro run; nada é gravado nas suas pastas.</div>`}`;
  const picked = new Set();
  const syncBulk = () => { $('#p-bulk').disabled = !picked.size; $('#p-bulk').textContent = picked.size ? `Adicionar selecionados (${picked.size})` : 'Adicionar selecionados'; };
  const bindPicks = () => $$('.pick').forEach(cb => {
    const key = `${cb.dataset.kind}:${cb.dataset.i}`;
    cb.checked = picked.has(key);
    cb.onchange = () => { cb.checked ? picked.add(key) : picked.delete(key); syncBulk(); };
  });
  const bindGithub = () => { $$('[data-github]').forEach(b => b.onclick = () => add(github[Number(b.dataset.github)].repo, b)); bindPicks(); };
  bindGithub();
  if ($('#p-filter')) $('#p-filter').oninput = () => { $('#p-github-rows').innerHTML = rows($('#p-filter').value.trim().toLowerCase()); bindGithub(); };
  $('#p-bulk').onclick = async () => {
    $('#p-bulk').disabled = true;
    const keys = [...picked];
    const results = [];
    for (const [n, key] of keys.entries()) {
      const [kind, i] = key.split(':');
      const r = kind === 'local' ? found.local[Number(i)] : github[Number(i)];
      msg(`adicionando ${n + 1} de ${keys.length}…`);
      try { await addOne(kind === 'local' ? r.path : r.repo, null); results.push(`✓ ${esc(r.repo)}`); picked.delete(key); }
      catch (e) { results.push(`<span class="err">✗ ${esc(r.repo)}: ${esc(e.message)}</span>`); }
      $('#p-results').innerHTML = results.join('<br>');
    }
    msg(`${results.filter(x => x.startsWith('✓')).length} de ${keys.length} adicionados`);
    refreshRail().catch(() => {});
    syncBulk();
  };
}

// settings (Phase 6): one form for the global layer and for a project's layer; Phase 7.1: in sections

const SETTING_LABELS = {
  'implementer.default_model': ['Modelo do implementador', 'modelo do Claude para etapas difíceis e runs sem plano'],
  'implementer.mechanical_model': ['Modelo do implementador, mecânico', 'modelo do Claude para etapas que o planejador marca como mecânicas'],
  'reviewer.codex_model': ['Modelo do planejador e do revisor', 'modelo do Codex; vale a partir da próxima chamada, inclusive em runs rodando'],
  'reviewer.claude_model': ['Modelo do revisor Claude', 'quando o revisor é o Claude (principal ou reserva depois de um limite do Codex)'],
  'reviewer.routine_effort': ['Esforço da revisão', 'esforço de raciocínio das revisões de cada etapa'],
  'reviewer.final_effort': ['Esforço do plano e da revisão final', 'esforço de raciocínio do plano e da revisão antes do merge'],
  'git.protected_paths': ['Caminhos protegidos', 'um glob por linha; o guard pergunta antes de qualquer mudança neles (vale quando um run começa)'],
  'guard.max_net_deleted_lines': ['Máximo de linhas apagadas', 'saldo por iteração antes de o guard de diff perguntar'],
  'guard.source_globs': ['Arquivos de código', 'um glob por linha; o que conta para a regra de linhas apagadas'],
  'notify.auto_answer': ['Respostas automáticas', 'quando você não responde, o orq aplica a recomendação em perguntas de baixo risco (nunca negócio, destrutivas, guard ou aprovação de plano)'],
  'notify.auto_answer_minutes': ['Minutos até a resposta automática', 'contados de quando o WhatsApp recebeu a pergunta'],
  'notify.auto_answer_max_stakes': ['Maior risco respondido automaticamente', 'low é a escolha segura'],
  'limits.max_concurrent_runs': ['Slots: runs ao mesmo tempo', 'todos os projetos juntos, de 1 a 6; o ChatGPT Plus tem limites apertados, então com mais runs o revisor passa para o Claude mais cedo'],
  'queue.project_concurrency': ['Runs ao mesmo tempo por projeto', 'padrão de cada projeto, de 1 a 6; um projeto pode definir o seu'],
  'projects.scan_roots': ['Pastas com seus repositórios', 'uma pasta por linha; "Adicionar projeto" lista os checkouts encontrados nelas (o orq só lê)'],
  'projects.scan_depth': ['Níveis de pasta para procurar', 'abaixo de cada uma dessas pastas'],
};
const SECTIONS = [
  ['models', 'Modelos', k => k.startsWith('implementer.') || k.startsWith('reviewer.')],
  ['guard', 'Guard', k => k.startsWith('git.') || k.startsWith('guard.')],
  ['answers', 'Respostas automáticas', k => k.startsWith('notify.')],
  ['slots', 'Execução', k => k.startsWith('limits.') || k.startsWith('queue.')],
  ['folders', 'Pastas', k => k.startsWith('projects.')],
];
const STAKES = ['low', 'medium', 'high'];
const SOURCE_LABELS = { config: 'config.toml', global: 'global', project: 'projeto', task: 'task' };

function settingInput(key, payload, layerValue, inherited) {
  const id = `set-${key.replace(/\./g, '-')}`;
  const shown = layerValue ?? '';
  const ph = Array.isArray(inherited) ? inherited.join('\n') : inherited;
  if (key === 'reviewer.codex_model') {
    const opts = payload.codex_models.map(m => `<option value="${esc(m.slug)}" ${m.slug === shown ? 'selected' : ''}>${esc(m.display_name)} (${esc(m.slug)})</option>`).join('');
    return `<select id="${id}" data-key="${key}"><option value="">herdar: ${esc(ph)}</option>${opts}</select>`;
  }
  if (key === 'notify.auto_answer') {
    const v = shown === '' ? '' : String(shown);
    return `<select id="${id}" data-key="${key}" data-bool="1"><option value="">herdar: ${esc(ph ? 'ligado' : 'desligado')}</option><option value="true" ${v === 'true' ? 'selected' : ''}>ligado</option><option value="false" ${v === 'false' ? 'selected' : ''}>desligado</option></select>`;
  }
  if (key === 'notify.auto_answer_max_stakes') {
    return `<select id="${id}" data-key="${key}"><option value="">herdar: ${esc(ph)}</option>${STAKES.map(s => `<option ${s === shown ? 'selected' : ''}>${s}</option>`).join('')}</select>`;
  }
  if (key.endsWith('_effort')) {
    return `<select id="${id}" data-key="${key}"><option value="">herdar: ${esc(ph)}</option>${payload.efforts.map(e => `<option ${e === shown ? 'selected' : ''}>${e}</option>`).join('')}</select>`;
  }
  if (Array.isArray(inherited)) {
    return `<textarea id="${id}" data-key="${key}" data-list="1" rows="3" placeholder="herdar:\n${esc(ph)}">${esc(Array.isArray(shown) ? shown.join('\n') : '')}</textarea>`;
  }
  if (typeof inherited === 'number') return `<input id="${id}" data-key="${key}" data-int="1" type="number" min="1" value="${esc(shown)}" placeholder="herdar: ${esc(ph)}">`;
  return `<input id="${id}" data-key="${key}" list="claude-models" value="${esc(shown)}" placeholder="herdar: ${esc(ph)}">`;
}

function settingRow(key, payload, layer, effective, sources, layerName) {
  const [label, hint] = SETTING_LABELS[key] || [key, ''];
  const src = sources[key];
  const test = key.endsWith('_model') ? `<button type="button" data-test="${key}">Testar</button><span class="msg small" data-test-msg="${key}"></span>` : '';
  const now = Array.isArray(effective[key]) ? effective[key].join(', ') : typeof effective[key] === 'boolean' ? (effective[key] ? 'ligado' : 'desligado') : effective[key];
  return `<div class="setting"><label>${esc(label)} <span class="chip">${esc(src === layerName ? 'definido aqui' : 'de ' + (SOURCE_LABELS[src] || src))}</span></label>
    <div style="display:flex;gap:6px;align-items:flex-start">${settingInput(key, payload, layer[key], effective[key])}${test}</div>
    <div class="hint">${esc(hint)} · agora: <span class="mono">${esc(now === '' || now == null ? '–' : now)}</span></div></div>`;
}

function settingsForm(payload, layer, effective, sources, layerName) {
  const keys = payload.keys.filter(k => layerName === 'task' ? payload.live_keys.includes(k)
    : layerName === 'project' ? !payload.global_only_keys.includes(k) : true);
  const datalist = `<datalist id="claude-models">${payload.claude_models.map(m => `<option value="${m}">`).join('')}</datalist>`;
  if (layerName === 'task') return datalist + keys.map(k => settingRow(k, payload, layer, effective, sources, layerName)).join('');
  const sections = SECTIONS.map(([id, title, test]) => [id, title, keys.filter(test)]).filter(([, , ks]) => ks.length);
  return `${datalist}<nav class="tabs subtabs">${sections.map(([id, title], i) => `<a href="javascript:void 0" data-section="${id}" class="${i ? '' : 'active'}">${title}</a>`).join('')}</nav>
    ${sections.map(([id, , ks], i) => `<div class="section" data-section-body="${id}" ${i ? 'hidden' : ''}>${ks.map(k => settingRow(k, payload, layer, effective, sources, layerName)).join('')}</div>`).join('')}`;
}

function bindSections(el) {
  $$('[data-section]', el).forEach(a => a.onclick = () => {
    $$('[data-section]', el).forEach(x => x.classList.toggle('active', x === a));
    $$('[data-section-body]', el).forEach(body => { body.hidden = body.dataset.sectionBody !== a.dataset.section; });
  });
}

function readSettings(el) {
  const out = {};
  $$('[data-key]', el).forEach(input => {
    const v = input.value.trim();
    if (!v) out[input.dataset.key] = null;
    else if (input.dataset.bool) out[input.dataset.key] = v === 'true';
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
    const model = input.value.trim() || (input.placeholder || '').replace(/^herdar: /, '');
    const kind = key === 'reviewer.codex_model' ? 'codex' : 'claude';
    const msg = $(`[data-test-msg="${key}"]`, el);
    msg.textContent = 'testando…'; msg.className = 'msg small';
    try { const r = await post('/api/models/test', { kind, model }); msg.textContent = r.ok ? `OK (${model})` : r.message; msg.className = `msg small ${r.ok ? '' : 'err'}`; }
    catch (e) { msg.textContent = e.message; msg.className = 'msg small err'; }
  });
}

async function globalSettingsPage() {
  const s = await api('/api/settings');
  view.innerHTML = `<div class="head"><div><h1>Configurações</h1><div class="muted small">Padrões de todos os projetos. Um projeto ou uma task pode mudar os que valem para ele. Modelos e esforços valem a partir da próxima chamada de agente, inclusive em runs rodando.</div></div></div>
    <div class="card" style="max-width:760px" id="gs">${settingsForm(s, s.overrides, s.effective, s.sources, 'global')}
      <div style="margin-top:12px"><button class="primary" id="gs-save">Salvar</button><span class="msg" id="gs-msg"></span></div></div>`;
  bindSections($('#gs'));
  bindModelTests($('#gs'));
  $('#gs-save').onclick = async () => {
    const open = $('#gs [data-section].active')?.dataset.section;
    try {
      await api('/api/settings', { method: 'PUT', body: JSON.stringify(readSettings($('#gs'))) });
      await route();
      if (open) $(`#gs [data-section="${open}"]`)?.click();
      $('#gs-msg').textContent = 'salvo';
    } catch (e) { $('#gs-msg').textContent = e.message; $('#gs-msg').className = 'msg err'; }
  };
}

// usage (Phase 7.1)

const pct = v => v == null ? '–' : `${Math.round(v)}%`;
const when = epoch => epoch ? new Date(epoch * 1000).toLocaleString('pt-BR', { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '–';

function usageTable(rows, key, label, fmt = x => esc(x)) {
  if (!rows.length) return '<div class="muted">Nada ainda.</div>';
  const max = Math.max(...rows.map(r => r.input_tokens + r.output_tokens), 1);
  return `<div class="bars usage-bars">${rows.map(r => `<span class="small">${fmt(r[key])}</span>
    <div class="track"><div class="fill" style="width:${Math.max(1, 100 * (r.input_tokens + r.output_tokens) / max)}%"></div></div>
    <span class="v">${num(r.input_tokens)} / ${num(r.output_tokens)} · ${r.calls} ${r.calls === 1 ? 'chamada' : 'chamadas'}</span>`).join('')}</div>
    <div class="hint">${esc(label)}: tokens de entrada / saída (a entrada inclui o cache do Claude)</div>`;
}

async function usageView(el, project) {
  const u = await api(`/api/usage${project ? `?project=${encodeURIComponent(project)}` : ''}`);
  const codex = u.limits.codex, claude = u.limits.claude;
  el.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${num(u.totals.input_tokens)}</b><span>tokens de entrada</span></div>
      <div class="kpi"><b>${num(u.totals.output_tokens)}</b><span>tokens de saída</span></div>
      <div class="kpi"><b>${num(u.totals.calls)}</b><span>chamadas de agente</span></div>
      <div class="kpi"><b>${num(u.totals.compactions)}</b><span>compactações de contexto</span></div>
    </div>
    <div class="card"><h3 class="muted small">LIMITES DOS PLANOS (CONTA TODA, ÚLTIMA LEITURA)</h3>
      <div class="grid2">
        <div><b>Codex (ChatGPT)</b>${codex ? `
          <div class="limit"><span>janela de 5 h</span><div class="track"><div class="fill" style="width:${Math.min(100, codex.primary?.used_percent || 0)}%"></div></div><span>${pct(codex.primary?.used_percent)} · reseta ${when(codex.primary?.resets_at)}</span></div>
          <div class="limit"><span>semana</span><div class="track"><div class="fill" style="width:${Math.min(100, codex.secondary?.used_percent || 0)}%"></div></div><span>${pct(codex.secondary?.used_percent)} · reseta ${when(codex.secondary?.resets_at)}</span></div>
          <div class="hint">lido ${ago(codex.at)}</div>` : '<div class="muted">sem leitura ainda</div>'}</div>
        <div><b>Claude (Max)</b>${claude ? `
          <div class="limit-text">${claude.status === 'allowed' ? 'liberado' : esc(claude.status)}${claude.rateLimitType ? ` · janela ${esc(claude.rateLimitType)}` : ''}${claude.resetsAt ? ` · reseta ${when(claude.resetsAt)}` : ''}</div>
          <div class="hint">lido ${ago(claude.at)}. O Claude não informa a porcentagem usada, só se está liberado.</div>` : '<div class="muted">sem leitura ainda</div>'}</div>
      </div></div>
    <div class="grid2">
      ${project ? '' : `<div class="card"><h3 class="muted small">POR PROJETO</h3>${usageTable(u.by_project, 'project', 'por projeto', x => `<a href="${projectPath(x)}/usage">${esc(x)}</a>`)}</div>`}
      <div class="card"><h3 class="muted small">POR DIA</h3>${usageTable(u.by_day, 'day', 'por dia', x => new Date(x + 'T12:00').toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', weekday: 'short' }))}</div>
      <div class="card"><h3 class="muted small">POR PAPEL</h3>${usageTable(u.by_role, 'role', 'por papel', x => esc(ROLE_LABELS[x] || x))}</div>
      <div class="card"><h3 class="muted small">POR MODELO</h3>${usageTable(u.by_model, 'model', 'por modelo', x => x === '?' ? '<span class="muted">não registrado (runs antigos)</span>' : `<span class="mono">${esc(x)}</span>`)}</div>
    </div>
    <div class="card"><h3 class="muted small">RUNS QUE MAIS GASTARAM</h3>
      ${u.runs.length ? `<table><thead><tr><th>run</th><th>task</th>${project ? '' : '<th>projeto</th>'}<th>estado</th><th>chamadas</th><th>entrada</th><th>saída</th><th>compactações</th></tr></thead><tbody>
        ${u.runs.slice(0, 25).map(r => `<tr class="click" data-run="${r.run_id}"><td class="id">${r.run_id}</td><td>${esc(r.title)}</td>${project ? '' : `<td class="muted mono small">${esc(r.project)}</td>`}
          <td>${stateChip(r.state)}</td><td class="mono">${r.calls}</td><td class="mono">${num(r.input_tokens)}</td><td class="mono">${num(r.output_tokens)}</td><td class="mono">${r.compactions}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">Nenhum run com chamadas ainda.</div>'}
      <div class="hint">Os detalhes de cada run ficam na aba Resumo do run. Uma compactação acontece quando o contexto do Claude enche: ele resume a conversa e continua na mesma sessão.</div></div>`;
  bindRunRows();
}

async function usagePage() {
  view.innerHTML = `<div class="head"><div><h1>Uso</h1><div class="muted small">Tokens de todos os projetos: runs e conversas. Os limites dos planos valem para a conta inteira.</div></div></div><div id="tab"></div>`;
  await usageView($('#tab'), null);
}

// chat (Phase 7.1)

function chatMessage(m) {
  const who = { user: 'você', assistant: 'Claude', error: 'erro' }[m.role] || m.role;
  const meta = m.role === 'assistant' && m.usage ? ` · ${esc(m.model || '')} · ${num((m.usage.input_tokens || 0) + (m.usage.cache_read_input_tokens || 0) + (m.usage.cache_creation_input_tokens || 0))} / ${num(m.usage.output_tokens)} tokens` : '';
  return `<div class="msgbox ${m.role}"><div class="who">${who}${m.via === 'whatsapp' ? ' <span class="chip">WhatsApp</span>' : ''} <span class="muted small">${new Date(m.ts).toLocaleTimeString('pt-BR')}${meta}</span></div><div class="text">${esc(m.text)}</div></div>`;
}

async function chatTab(el, p, chatId) {
  const chats = await api(`/api/projects/${p.repo}/chats`);
  const current = chatId ? await api(`/api/projects/${p.repo}/chats/${chatId}`).catch(() => null) : null;
  el.innerHTML = `<div class="chat">
      <div class="card chat-list" style="padding:0">
        <a href="${projectPath(p.repo)}/chat" class="${current ? '' : 'active'}"><b>+ Nova conversa</b></a>
        ${chats.map(c => `<a href="${projectPath(p.repo)}/chat/${c.chat_id}" class="${current && current.chat_id === c.chat_id ? 'active' : ''}">${esc(c.title)}
          <div class="muted small">${ago(c.updated_at)} · ${esc(c.model)}${c.via === 'whatsapp' ? ' · WhatsApp' : ''}</div></a>`).join('')}
      </div>
      <div class="card chat-main">
        <div class="hint">O Claude lê o código deste projeto (a versão mais recente de ${esc(p.base_branch)} no GitHub) e responde. Ele só lê: não altera arquivos nem inicia runs. Cada mensagem gasta da cota do Claude e aparece em Uso.</div>
        <div id="chat-log">${current ? current.messages.map(chatMessage).join('') : '<div class="empty">Pergunte algo sobre o projeto, por exemplo: "como funciona o cadastro de jogadores?"</div>'}</div>
        <textarea id="chat-input" rows="3" placeholder="Sua pergunta (Ctrl+Enter envia)"></textarea>
        <div style="display:flex;gap:6px;align-items:center;margin-top:6px">
          <select id="chat-model" style="width:auto">${['opus', 'sonnet'].map(m => `<option ${m === (current?.model || 'opus') ? 'selected' : ''}>${m}</option>`).join('')}</select>
          <button class="primary" id="chat-send">Enviar</button>
          ${current ? '<button class="danger" id="chat-delete">Apagar conversa</button>' : ''}
          <span class="msg" id="chat-msg"></span>
        </div>
      </div></div>`;
  const log = $('#chat-log');
  log.scrollTop = log.scrollHeight;
  const send = async () => {
    const text = $('#chat-input').value.trim();
    if (!text) return;
    $('#chat-send').disabled = true;
    if (!current) log.innerHTML = '';
    log.insertAdjacentHTML('beforeend', chatMessage({ role: 'user', text, ts: new Date().toISOString() }) + '<div class="msgbox pending">o Claude está lendo o código…</div>');
    log.scrollTop = log.scrollHeight;
    $('#chat-input').value = '';
    try {
      const r = await post(`/api/projects/${p.repo}/chats`, { message: text, chat_id: current?.chat_id || null, model: $('#chat-model').value });
      if (location.hash.startsWith(`${projectPath(p.repo)}/chat`)) {
        const target = `${projectPath(p.repo)}/chat/${r.chat_id}`;
        if (location.hash === target) route(); else location.hash = target;
      }
    } catch (e) {
      $('.msgbox.pending', log)?.remove();
      $('#chat-msg').textContent = e.message; $('#chat-msg').className = 'msg err';
      $('#chat-send').disabled = false;
    }
  };
  $('#chat-send').onclick = send;
  $('#chat-input').onkeydown = e => { if (e.key === 'Enter' && e.ctrlKey) send(); };
  if ($('#chat-delete')) $('#chat-delete').onclick = async () => {
    if (!confirm('Apagar esta conversa?')) return;
    await api(`/api/projects/${p.repo}/chats/${current.chat_id}`, { method: 'DELETE' });
    location.hash = `${projectPath(p.repo)}/chat`;
  };
}

// run page

async function runPage(id, tab) {
  const d = await api(`/api/runs/${id}`);
  $('#wa').textContent = d.whatsapp ? 'WhatsApp ligado' : 'WhatsApp desligado (só o dashboard)';
  const finished = ['DONE', 'FAILED', 'ABORTED'].includes(d.state);
  const tabs = [['live', 'Ao vivo'], ['summary', 'Resumo'], ['replay', 'Replay']]
    .map(([k, label]) => `<a href="#/run/${id}/${k}" class="${k === tab ? 'active' : ''}">${label}</a>`).join('');
  const plan = d.plan && d.plan.milestones ? `<div class="muted small">plano: ${d.plan.milestones.map((m, i) => `${i + 1}. ${esc(m.title)} [${m.difficulty}]`).join(' · ')}</div>` : '';
  const decisions = d.decisions.map(x => `
    <div class="card decision ${x.destructive ? 'destructive' : ''}" data-decision="${x.decision_id}">
      <div><b class="mono">${x.decision_id}</b> <span class="muted">${esc(x.source)} · ${esc(x.decision_type)}${x.destructive ? ' · destrutiva' : ''}</span></div>
      ${x.context ? `<div class="ctx">${esc(x.context)}</div>` : ''}
      <div class="q"><b>${esc(x.question)}</b></div>
      ${x.option_details && x.option_details.length ? `<ol class="opts" start="0">${x.options.map((o, i) => `<li><b>${esc(o)}</b>${x.option_details[i] ? `: ${esc(x.option_details[i])}` : ''}</li>`).join('')}</ol>` : ''}
      ${x.recommendation_reason && x.recommendation != null ? `<div class="hint">Recomendado: ${esc(x.options[x.recommendation] ?? '')}, porque ${esc(x.recommendation_reason)}</div>` : ''}
      <div>${x.options.map((o, i) => `<button data-answer="${esc(o)}" class="${x.destructive && o === 'deny' ? 'danger' : ''}">${i}. ${esc(o)}${x.recommendation === i ? ' ★' : ''}</button>`).join('')}</div>
      <div style="margin-top:8px;display:flex;gap:6px"><input type="text" placeholder="resposta em texto livre"><button data-send>Enviar</button></div>
    </div>`).join('');
  view.innerHTML = `
    <div class="head"><div>
      <h1><span class="id">${d.run_id}</span> ${stateChip(d.state)} ${esc(d.task_title)}</h1>
      <div class="muted small"><a href="${projectPath(d.repo)}/runs" class="mono">${esc(d.repo)}</a> · ${esc(d.branch)} · iteração ${d.iteration} · fase ${esc(d.phase)}${d.pr_url ? ` · <a href="${d.pr_url}" target="_blank">PR</a>` : ''}</div>
      ${plan}
      <div style="margin:8px 0">
        ${finished ? '' : '<button data-act="pause">Pausar</button><button data-act="resume">Retomar</button><button data-act="abort" class="danger">Abortar</button>'}
        <button data-act="rerun">Rodar de novo</button><span class="msg" id="msg"></span>
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
    <div class="pane card"><h3>progresso <a href="javascript:void 0" id="digest-refresh" class="small">atualizar</a></h3><pre id="digest" class="muted">carregando…</pre></div>
    <div class="pane card"><h3>eventos</h3><pre id="events"></pre></div>
    <div class="grid2">
      <div class="pane card"><h3>implementador (claude)</h3><pre id="impl"></pre></div>
      <div class="pane card"><h3>revisor</h3><pre id="rev"></pre></div>
    </div>`;
  const loadDigest = async () => {
    try { const d = await api(`/api/runs/${id}/digest`); $('#digest').textContent = d.text.replace(/^\*|\*$/gm, ''); $('#digest').classList.remove('muted'); }
    catch (e) { $('#digest').textContent = e.message; }
  };
  $('#digest-refresh').onclick = loadDigest;
  loadDigest();
  attach(`/api/runs/${id}/events`, $('#events'));
  attach(`/api/runs/${id}/stream/implementer`, $('#impl'));
  attach(`/api/runs/${id}/stream/reviewer`, $('#rev'));
}

async function summaryTab(el, id) {
  const s = await api(`/api/runs/${id}/summary`);
  const totalState = Object.values(s.time_by_state).reduce((a, b) => a + b, 0) || 1;
  const agentRows = Object.entries(s.agents).filter(([, a]) => a.calls).map(([role, a]) => `<tr><td>${esc(ROLE_LABELS[role] || role)}</td><td>${a.calls}${a.failed ? ` <span class="muted">(${a.failed} falharam)</span>` : ''}</td>
    <td class="mono">${dur(a.seconds)}</td><td class="mono">${num(a.input_tokens)}</td><td class="mono">${num(a.output_tokens)}</td></tr>`).join('');
  el.innerHTML = `
    <div class="kpis">
      <div class="kpi"><b>${dur(s.wall_seconds)}</b><span>tempo total</span></div>
      <div class="kpi"><b>${s.iterations}</b><span>iterações</span></div>
      <div class="kpi"><b>${s.milestones.done}/${s.milestones.planned}</b><span>etapas</span></div>
      <div class="kpi"><b>${s.decisions.length}</b><span>decisões</span></div>
      <div class="kpi"><b>${s.checks.runs - s.checks.failed}/${s.checks.runs}</b><span>checagens ok</span></div>
      <div class="kpi"><b>${s.pr.merged ? 'merge feito' : s.pr.url ? 'aberto' : '–'}</b><span>pull request</span></div>
    </div>
    <div class="grid2">
      <div class="card"><h3 class="muted small">TEMPO POR ESTADO</h3><div class="bars">
        ${Object.entries(s.time_by_state).sort((a, b) => b[1] - a[1]).map(([state, sec]) => `<span class="small">${esc(STATE_LABELS[state] || state)}</span>
          <div class="track"><div class="fill ${state}" style="width:${Math.max(1, 100 * sec / totalState)}%"></div></div><span class="v">${dur(sec)}</span>`).join('')}
      </div></div>
      <div class="card"><h3 class="muted small">AGENTES</h3>
        <table><thead><tr><th>papel</th><th>chamadas</th><th>tempo</th><th>tokens entrada</th><th>tokens saída</th></tr></thead><tbody>${agentRows || '<tr><td colspan="5" class="muted">nenhum</td></tr>'}</tbody></table>
        <div class="hint">modelos: ${esc([...new Set(s.implementer_models)].join(', ') || '–')} · trocas de revisor: ${s.reviewer_switches}
        ${s.ci ? ` · CI ${esc(s.ci.state)} depois de ${dur(s.ci.waited_seconds)}${s.ci.reruns ? `, ${s.ci.reruns} nova(s) execução(ões)` : ''}` : ''}
        · guard: ${s.guard.pre_denials} bloqueio(s), ${s.guard.diff_rule_hits} alerta(s) de diff</div>
      </div>
    </div>
    <div class="card"><h3 class="muted small">DECISÕES</h3>
      ${s.decisions.length ? `<table><thead><tr><th>id</th><th>de</th><th>pergunta</th><th>resposta</th><th>respondida por</th><th>espera</th></tr></thead><tbody>
        ${s.decisions.map(x => `<tr><td class="mono">${x.decision_id}</td><td class="muted">${esc(x.source)}</td><td>${esc(x.question)}</td>
        <td>${esc(x.answer ?? 'pendente')}</td><td class="muted">${esc(BY_LABELS[x.by] ?? x.by ?? '')}${x.via ? ' · ' + esc(x.via) : ''}</td><td class="mono">${dur(x.waited_seconds)}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">Nenhuma. O run não precisou de você.</div>'}
    </div>
    <div class="card"><h3 class="muted small">ARQUIVOS ALTERADOS</h3><div class="mono small">${s.files_changed.map(esc).join('<br>') || '<span class="muted">nenhum</span>'}</div></div>`;
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
        <div class="muted small">${s.events.length} eventos${s.events[0] ? ' · ' + new Date(s.events[0].ts).toLocaleTimeString('pt-BR') : ''}</div></a>`).join('')}</div>
      <div><div class="card"><div class="artifacts" id="arts"></div><div id="art"></div></div>
        <div class="card"><h3 class="muted small">EVENTOS</h3><div id="evts"></div></div></div>
    </div>`;
  const show = async (i) => {
    $$('[data-step]', el).forEach(a => a.classList.toggle('active', Number(a.dataset.step) === i));
    const step = steps[i];
    $('#evts').innerHTML = step.events.map(e => {
      const { ts, type, ...rest } = e;
      const detail = Object.entries(rest).filter(([k]) => !['usage', 'rate_limit', 'session_id'].includes(k))
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
      return `<div class="evt"><span class="t">${new Date(ts).toLocaleTimeString('pt-BR')}</span><b>${esc(type)}</b> ${esc(detail.slice(0, 400))}</div>`;
    }).join('') || '<div class="muted">nenhum evento</div>';
    $('#arts').innerHTML = step.artifacts.map(a => `<button data-art="${esc(a)}">${esc(a.split('/').pop())}</button>`).join('') || '<span class="muted">nenhum arquivo registrado</span>';
    $('#art').innerHTML = '';
    const open = async (path) => {
      $$('[data-art]', el).forEach(b => b.classList.toggle('active', b.dataset.art === path));
      const r = await api(`/api/runs/${id}/artifact?path=${encodeURIComponent(path)}`);
      $('#art').innerHTML = (r.truncated ? '<div class="hint">mostrando o fim de um arquivo longo</div>' : '') + renderArtifact(path, r.text);
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
  try { await post(`/api/decisions/${decisionId}/answer`, { answer: text, by: 'owner' }); $('#msg').textContent = `${decisionId} respondida: ${text}`; }
  catch (e) { $('#msg').textContent = e.message; $('#msg').className = 'msg err'; }
  setTimeout(route, 500);
}

const ACT_LABELS = { pause: 'pausar', resume: 'retomar', abort: 'abortar', rerun: 'rodar de novo' };

async function act(runId, what) {
  if (what === 'abort' && !confirm(`Abortar ${runId}? A worktree dele é removida.`)) return;
  try {
    const r = await post(`/api/runs/${runId}/${what}`);
    if (what === 'rerun') { location.hash = `#/run/${r.run_id}`; return; }
    $('#msg').textContent = `${ACT_LABELS[what] || what}: ok${r.pid ? ' (pid ' + r.pid + ')' : ''}${r.warning ? ' · ' + r.warning : ''}`;
  } catch (e) { $('#msg').textContent = e.message; $('#msg').className = 'msg err'; }
  setTimeout(route, 800);
}

// periodic refresh: the rail always; list views when nothing is being typed

setInterval(() => {
  $('#clock').textContent = new Date().toLocaleTimeString('pt-BR');
  refreshRail().catch(() => {});
  const typing = document.activeElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
  const editing = $('#qedit') && !$('#qedit').hidden;
  if (!typing && !editing && /^#\/(all|p\/[^/]+\/[^/]+\/(runs|queue))$/.test(location.hash) && routeKey === location.hash) route();
}, 5000);

route();
