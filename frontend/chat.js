'use strict';

const $ = id => document.getElementById(id);
const TOKEN = 'gaokao_auth_token';
const USER = 'gaokao_auth_user';
const TERMINAL = new Set(['completed', 'waiting', 'degraded', 'failed', 'cancelled', 'insufficient_data']);
const STATUS = {
  queued: '排队中', running: '研究中', waiting: '待确认', completed: '已完成',
  degraded: '已到执行上限', failed: '未完成', cancelled: '已停止', insufficient_data: '资料不足',
};
const ROLES = {coordinator: '协调', major_research: '专业研究', admissions: '招生研究', planner: '志愿规划', reviewer: '方案审核'};
const FIELDS = {interest: '兴趣', career_goal: '职业方向', learning_preference: '学习偏好', excluded_city: '排除城市', annual_budget_max: '年度预算上限'};
const ELIGIBILITY = {eligible: '条件符合', ineligible: '条件不符', unknown: '待核实', conflicting: '资料冲突'};
const SUBJECTS = ['物理', '历史', '化学', '生物', '地理', '政治'];
const TOOLS = {
  get_profile: '读取考生条件', query_candidates: '查询历史录取', search_sources: '查找公开来源',
  get_candidate_details: '读取候选详情', compare_candidates: '比较候选',
  analyze_historical_risk: '分析历史位次', submit_report: '保存研究报告',
  submit_plan: '提交方案审核', validate_plan: '检查方案条件',
};

let account = null;
let authEpoch = 0;
let selection = 0;
let sid = null;
let current = null;
let sessions = [];
let activeRun = null;
let submitting = false;
let cancelling = false;
let watcher = null;
let listRequest = 0;
let detailsRequest = 0;
let knowledgeRequest = 0;
let knowledge = {profile: {}, memories: []};
let coverage = [];
let showingArchive = false;
let authMode = 'login';
let authPending = false;
let historyRequest = 0;
let historyQuery = null;
let currentArtifact = null;
const comparison = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action, className = 'quiet') {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function feedback(id, text = '') {
  $(id).textContent = text;
  $(id).hidden = !text;
}

function notify(text = '') {
  feedback('notice', text);
}

function handleError(error) {
  if (error.name !== 'AbortError') notify(error.message);
}

function key(name, id = sid) {
  return `gaokao_v2_${account.id}_${name}${id ? '_' + id : ''}`;
}

function stored(name) {
  try { return JSON.parse(localStorage.getItem(name)); } catch { return null; }
}

function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function errorText(detail, fallback) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => {
      const message = item.msg?.replace(/^Value error, /, '');
      return message && /[\u3400-\u9fff]/.test(message) ? message : fallback;
    }).filter((text, i, all) => all.indexOf(text) === i).join('。') || fallback;
  }
  return fallback;
}

async function request(path, {method = 'GET', body, auth = true} = {}) {
  const epoch = authEpoch;
  const token = localStorage.getItem(TOKEN);
  const headers = {};
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15000), cache: 'no-store'});
  } catch (error) {
    if (epoch !== authEpoch) throw new DOMException('', 'AbortError');
    throw new Error(error.name === 'TimeoutError' ? '连接超时，请重试。' : '无法连接服务，请检查网络后重试。');
  }
  if (epoch !== authEpoch) throw new DOMException('', 'AbortError');
  if (auth && response.status === 401) {
    clearAccount(token);
    throw new Error('登录已失效，请重新登录。');
  }
  const data = await response.json().catch(() => null);
  if (epoch !== authEpoch) throw new DOMException('', 'AbortError');
  if (!response.ok || data === null) {
    const error = new Error(errorText(data?.detail, response.status === 422 ? '请检查输入内容。' : '服务暂时无法处理请求，请重试。'));
    error.status = response.status;
    throw error;
  }
  return data;
}

const api = (path, options) => request('/api/v2/agent' + path, options);
const sessionPath = id => '/sessions/' + encodeURIComponent(id);
const selectedSession = () => sessions.find(session => session.id === sid);
const selectedRun = () => activeRun?.session_id === sid ? activeRun : null;
const currentSelection = (epoch, version) => epoch === authEpoch && version === selection;

function changeUrl(id, replace = false, removeProfile = false) {
  const url = new URL(location.href);
  if (id) url.searchParams.set('session', id);
  else url.searchParams.delete('session');
  if (removeProfile) url.searchParams.delete('profile');
  history[replace ? 'replaceState' : 'pushState'](null, '', url);
}

function clearAccount(token, stripSession = false) {
  if (token && localStorage.getItem(TOKEN) === token) {
    localStorage.removeItem(TOKEN);
    localStorage.removeItem(USER);
  }
  authEpoch++;
  watcher?.controller.abort();
  watcher = null;
  account = null;
  current = null;
  sid = null;
  sessions = [];
  activeRun = null;
  submitting = cancelling = false;
  showingArchive = false;
  knowledge = {profile: {}, memories: []};
  coverage = [];
  historyQuery = currentArtifact = null;
  comparison.clear();
  renderComparison();
  $('workspace').hidden = $('loading').hidden = $('logout').hidden = $('account-name').hidden = true;
  $('auth').hidden = false;
  $('account-name').textContent = '';
  $('session-search').value = $('message').value = '';
  $('profile-form').reset();
  closePanel('sidebar');
  closePanel('inspector');
  $('session-form').reset();
  $('history-form').reset();
  for (const id of ['messages', 'sessions', 'artifacts', 'memories', 'artifact-content', 'history-results', 'confirmations', 'progress-steps']) $(id).replaceChildren();
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  setAuthMode('login');
  if (stripSession) changeUrl(null, true, true);
}

function setAuthMode(mode) {
  authMode = mode;
  const register = mode === 'register';
  $('auth-title').textContent = register ? '创建账号' : '登录志愿研究室';
  $('auth-submit').textContent = register ? '创建并登录' : '登录';
  $('auth-switch').textContent = register ? '返回登录' : '创建账号';
  $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('password').value = $('password-confirm').value = '';
  $('password-confirm').hidden = $('password-confirm-label').hidden = $('auth-rules').hidden = !register;
  $('password-confirm').required = register;
  feedback('auth-error');
}

async function authenticate(event) {
  event.preventDefault();
  if (authPending) return;
  const username = $('username').value.trim();
  const password = $('password').value;
  if (authMode === 'register' && password !== $('password-confirm').value) {
    feedback('auth-error', '两次输入的密码不一致。');
    $('password-confirm').focus();
    return;
  }
  authPending = true;
  $('auth-submit').disabled = $('auth-switch').disabled = true;
  feedback('auth-error');
  notify();
  try {
    const result = await request('/api/v1/auth/' + authMode, {method: 'POST', body: {username, password}, auth: false});
    localStorage.setItem(TOKEN, result.data.token);
    localStorage.setItem(USER, JSON.stringify(result.data.user));
    $('password').value = $('password-confirm').value = '';
    await start();
  } catch (error) {
    if (error.name !== 'AbortError') feedback('auth-error', error.message);
  } finally {
    authPending = false;
    $('auth-submit').disabled = $('auth-switch').disabled = false;
  }
}

function renderSessions() {
  const search = $('session-search').value.trim().toLocaleLowerCase();
  const visible = sessions.filter(session => session.archived === showingArchive && (session.title || '新对话').toLocaleLowerCase().includes(search));
  $('show-archive').setAttribute('aria-pressed', showingArchive);
  $('show-archive').textContent = showingArchive ? '返回对话' : '归档';
  $('sessions').replaceChildren();
  if (!visible.length) $('sessions').append(element('p', search ? '没有匹配的对话' : showingArchive ? '暂无归档' : '暂无对话', 'empty'));
  for (const session of visible) {
    const title = session.title || '新对话';
    const item = button(title, () => selectSession(session.id).catch(handleError), 'session-item');
    item.title = title;
    if (session.running || activeRun?.session_id === session.id) {
      item.prepend(element('span', '● ', 'run-dot'));
      item.setAttribute('aria-label', title + '，研究中');
    }
    if (session.id === sid) item.setAttribute('aria-current', 'page');
    $('sessions').append(item);
  }
}

async function loadSessions() {
  const index = ++listRequest;
  const data = await api('/sessions');
  if (index !== listRequest) return;
  sessions = data.sessions;
  if (data.active_run) setActive(data.active_run);
  renderSessions();
  renderControls();
}

function setDraft(text) {
  $('message').value = text;
  if (text) localStorage.setItem(key('draft'), text);
  else {
    localStorage.removeItem(key('draft'));
    localStorage.removeItem(key('history_candidate'));
  }
  renderControls();
}

async function selectSession(id, {replace = false, writeUrl = true, focus = false} = {}) {
  const epoch = authEpoch;
  const version = ++selection;
  const session = sessions.find(item => item.id === id);
  if (session) showingArchive = session.archived;
  sid = id;
  current = null;
  $('message').value = localStorage.getItem(key('draft')) || '';
  if (writeUrl) changeUrl(id, replace);
  localStorage.setItem(key('session', null), id || '');
  renderSessions();
  renderConversation(true);
  closePanel('sidebar');
  if (id) await refreshSession();
  if (focus && currentSelection(epoch, version)) $('message').focus();
}

async function refreshSession() {
  if (!sid) return;
  const epoch = authEpoch;
  const version = selection;
  const index = ++detailsRequest;
  const data = await api(sessionPath(sid));
  if (!currentSelection(epoch, version) || index !== detailsRequest) return;
  current = data;
  const pendingKey = key('submission');
  const pending = stored(pendingKey);
  if (pending && data.runs.some(run => run.request_id === pending.request_id)) {
    acknowledge(pending, pendingKey, key('draft'), key('history_candidate'));
  }
  const run = data.runs.find(item => !TERMINAL.has(item.status));
  if (run) setActive(run);
  renderConversation();
}

function statusOf(run) {
  return run?.cancel_requested && !TERMINAL.has(run.status) ? '正在停止' : STATUS[run?.status] || '';
}

function renderControls() {
  const session = selectedSession();
  const run = selectedRun();
  const previous = current?.runs[0];
  const hasConfirmations = Object.values(current?.state.pending || {}).some(item => item.profile_version === current.state.profile_version);
  const status = session?.archived ? '已归档' : statusOf(run || (previous?.status === 'waiting' && !hasConfirmations ? null : previous));
  $('conversation-title').textContent = session?.title || '新对话';
  feedback('task-status', status);
  $('other-run').hidden = !activeRun || !!run;
  $('session-settings').hidden = !session;
  $('message-form').hidden = !!session?.archived;
  $('archived-composer').hidden = !session?.archived;
  $('send').disabled = submitting || !!activeRun || !$('message').value.trim() || (!!sid && !current);
  $('send').textContent = submitting ? '正在提交…' : '发送 ↑';
  const reference = account && stored(key('history_candidate'));
  const hasReference = reference?.prompt && $('message').value.includes(reference.prompt);
  $('composer-hint').textContent = activeRun ? '研究结束后可发送，草稿会自动保存。' :
    hasReference ? (reference.plan && $('message').value.trim() === reference.prompt ? '为所选候选拟一份研究草案' : '已关联所选历年录取候选') :
    'Enter 发送 · Shift + Enter 换行';
  $('edit-profile').disabled = !!activeRun;
  $('cancel').disabled = !run || !!run.cancel_requested || cancelling;
  $('cancel').hidden = !run;
  $('cancel').textContent = run?.cancel_requested ? '正在停止…' : '停止';
  for (const control of $('confirmations').querySelectorAll('button')) control.disabled = !!activeRun || submitting;
  for (const control of $('memories').querySelectorAll('input,button')) control.disabled = !!activeRun;
}

function renderConversation(forceScroll = false) {
  const log = $('messages');
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const oldTop = log.scrollTop;
  const state = current?.state;
  const conversation = state?.conversation || [];
  const run = selectedRun();
  log.replaceChildren();
  if (!conversation.length && !run) {
    const welcome = element('div', null, 'welcome');
    welcome.append(element('h2', sid && !current ? '正在读取对话…' : '今天想研究什么？'));
    if (!sid || current) {
      welcome.append(element('p', '可以先聊兴趣和方向。有了目标院校或专业，也可以直接查历年录取。'));
      const suggestions = element('div', null, 'suggestions');
      for (const [label, prompt] of [
        ['探索专业方向', '我还没有明确的专业方向，可以从我的兴趣开始问我吗？'],
        ['比较两个专业', '计算机和电子信息在课程、就业方向和适合的人群上有什么区别？'],
        ['研究志愿方案', '请根据我的考生条件研究志愿方案，并列出还需要核实的资格和招生计划。'],
      ]) suggestions.append(button(label, () => {
        if (appendDraft(prompt)) $('message').focus();
      }, 'secondary'));
      welcome.append(suggestions);
    }
    log.append(welcome);
  }
  for (const message of conversation) log.append(messageNode(message.role, message.content));
  if (run && !current?.input_saved && !current?.runs.some(item => item.id === run.id && TERMINAL.has(item.status))) {
    log.append(messageNode('user', run.input_text));
  }
  const latest = current?.runs[0];
  if (latest && !run && ['failed', 'degraded', 'cancelled'].includes(latest.status)) {
    const actions = element('div', null, 'actions');
    const retry = current.runs.find(item => item.input_text && !item.confirmed);
    const canRetryConfirmation = latest.confirmed && Object.values(state?.pending || {}).some(
      item => item.retry_after_provider_error && item.profile_version === state.profile_version);
    if (retry && !canRetryConfirmation && !selectedSession()?.archived) {
      actions.append(button('将问题放回输入框', () => {
        if (appendDraft(retry.input_text)) $('message').focus();
      }, 'secondary'));
    }
    actions.append(button('查历年录取', openHistory, 'secondary'));
    log.append(actions);
  }
  $('execution').hidden = !(run || latest);
  if (!run && latest) {
    $('progress-text').textContent = statusOf(latest);
    loadMetrics(latest.id, selection).catch(() => {});
  } else if (run) renderProgress(watcher);
  renderConfirmations(state);
  renderArtifacts(state);
  renderProfileSummary();
  renderControls();
  log.scrollTop = forceScroll || nearBottom ? log.scrollHeight : oldTop;
}

function messageNode(role, text) {
  const row = element('article', null, 'message ' + (role === 'user' ? 'user' : 'assistant'));
  const label = element('div', null, 'message-label');
  label.append(element('strong', role === 'user' ? '你' : '志愿研究助手'), button('复制', () => copy(text).catch(handleError)));
  row.append(label);
  const content = element('div', null, 'message-text');
  if (role === 'user') content.textContent = text;
  else appendAnswer(content, text || '');
  row.append(content);
  return row;
}

function safeLink(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : null;
  } catch { return null; }
}

function appendAnswer(container, text) {
  for (const line of text.split('\n')) {
    const heading = /^(#{1,4}) (.+)$/.exec(line);
    const row = element(heading ? 'h3' : 'div', null, heading ? '' : 'answer-line');
    const formatted = (heading ? heading[2] : line).replace(/^\s*[-*] /, '• ');
    for (const part of formatted.split(/(\*\*[^*]+\*\*|\[[^\]]+\]\(https:\/\/[^\s)]+\))/g)) {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(part);
      const href = link && safeLink(link[2]);
      if (href) {
        const anchor = element('a', link[1]);
        anchor.href = href;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        row.append(anchor);
      } else if (part.startsWith('**') && part.endsWith('**')) row.append(element('strong', part.slice(2, -2)));
      else row.append(document.createTextNode(part));
    }
    container.append(row);
  }
}

function renderConfirmations(state) {
  const container = $('confirmations');
  container.replaceChildren();
  const entries = Object.entries(state?.pending || {}).filter(([, item]) => item.profile_version === state.profile_version);
  container.hidden = !entries.length || !!selectedSession()?.archived;
  for (const [id, item] of entries) {
    const description = item.kind === 'research' ? item.text : `记录${FIELDS[item.field] || item.field}：${item.value}`;
    const card = element('div', null, 'confirmation');
    const actions = element('div', null, 'actions');
    const retry = item.retry_after_provider_error;
    actions.append(button(retry ? '重试研究' : '确认并继续', () => send('确认：' + description, [id]).catch(handleError), 'primary'),
      button('略过', () => dismissConfirmation(id).catch(handleError)));
    card.append(element('strong', retry ? '研究尚未开始，可重试原范围' : '待你确认'), element('p', description), actions);
    container.append(card);
  }
}

async function dismissConfirmation(id) {
  await api(sessionPath(sid) + '/confirmations/' + encodeURIComponent(id), {method: 'DELETE'});
  await refreshSession();
}

function acknowledge(pending, submissionKey, draftKey, historyKey) {
  if (stored(submissionKey)?.request_id !== pending.request_id) return;
  localStorage.removeItem(submissionKey);
  if (!pending.content.confirmed_ids.length && (localStorage.getItem(draftKey) || '').trim() === pending.content.text) {
    localStorage.removeItem(draftKey);
    localStorage.removeItem(historyKey);
    if (account && draftKey === key('draft') && $('message').value.trim() === pending.content.text) $('message').value = '';
  }
}

async function send(raw, confirmedIds = []) {
  const text = raw.trim();
  if (!text || submitting || activeRun || selectedSession()?.archived || (sid && !current)) return;
  const epoch = authEpoch;
  submitting = true;
  notify();
  renderControls();
  try {
    if (!sid) {
      const version = selection;
      const emptyDraft = key('draft');
      const emptyHistory = key('history_candidate');
      const creationKey = key('create', null);
      const request_id = localStorage.getItem(creationKey) || requestId();
      localStorage.setItem(creationKey, request_id);
      const data = await api('/sessions', {method: 'POST', body: {request_id}});
      if (!currentSelection(epoch, version)) return;
      const textInDraft = localStorage.getItem(emptyDraft);
      if (textInDraft) {
        localStorage.setItem(key('draft', data.id), textInDraft);
        localStorage.removeItem(emptyDraft);
      }
      const selectedHistory = localStorage.getItem(emptyHistory);
      if (selectedHistory) {
        localStorage.setItem(key('history_candidate', data.id), selectedHistory);
        localStorage.removeItem(emptyHistory);
      }
      if (!sessions.some(session => session.id === data.id)) {
        sessions.unshift({id: data.id, title: '', archived: false, running: false});
      }
      const selected = selectSession(data.id);
      localStorage.removeItem(creationKey);
      await selected;
      if (sid !== data.id || !currentSelection(epoch, version + 1)) return;
    }
    const target = sid;
    const submissionKey = key('submission', target);
    const draftKey = key('draft', target);
    const historyKey = key('history_candidate', target);
    const content = {text, confirmed_ids: confirmedIds};
    const history = stored(historyKey);
    if (!confirmedIds.length && history?.prompt && text.includes(history.prompt)) {
      content.history_candidate_id = history.id;
      if (history.plan && text === history.prompt) content.history_plan = true;
    }
    const saved = stored(submissionKey);
    const pending = JSON.stringify(saved?.content) === JSON.stringify(content) ? saved : {request_id: requestId(), content};
    localStorage.setItem(submissionKey, JSON.stringify(pending));
    const run = await api(sessionPath(target) + '/messages', {method: 'POST', body: {...content, request_id: pending.request_id}});
    acknowledge(pending, submissionKey, draftKey, historyKey);
    if (!TERMINAL.has(run.status)) setActive(run);
    await Promise.all([loadSessions(), refreshSession()]);
  } catch (error) {
    if (error.status === 409 || error.status === 422) await Promise.allSettled([loadSessions(), refreshSession()]);
    throw error;
  } finally {
    if (epoch === authEpoch) {
      submitting = false;
      renderControls();
    }
  }
}

const finishedRuns = new Set();

function setActive(run) {
  if (run && finishedRuns.has(run.id)) return;
  if (!run) {
    if (activeRun) finishedRuns.add(activeRun.id);
    activeRun = null;
    watcher?.controller.abort();
    watcher = null;
    renderControls();
    return;
  }
  const changed = activeRun?.id !== run.id;
  if (changed || run.updated_at >= activeRun.updated_at) {
    activeRun = activeRun?.id === run.id && activeRun.cancel_requested ? {...run, cancel_requested: true} : run;
  }
  if (watcher?.id !== run.id) {
    watcher?.controller.abort();
    watcher = {id: run.id, controller: new AbortController(), steps: new Map(), cursor: 0, connected: true, epoch: authEpoch};
    watch(watcher).catch(handleError);
  }
  if (changed) renderConversation();
  renderControls();
}

function stepLabel(step) {
  const action = step.kind === 'model' ? '整理分析' : TOOLS[step.name] || '处理研究资料';
  return `${ROLES[step.role] || '研究'} · ${action} · ${step.ok ? '完成' : '需处理'}`;
}

function renderProgress(stream) {
  const run = selectedRun();
  if (!run) return;
  const step = stream ? [...stream.steps.values()].at(-1) : null;
  const text = run.cancel_requested ? '正在停止研究…' :
    stream?.connected === false ? '进度连接中断，正在重连…' :
    run.status === 'queued' ? '已提交，等待执行…' :
    step ? stepLabel(step) : '正在研究，离开页面后仍会继续。';
  $('progress-text').textContent = text;
  $('progress-steps').replaceChildren(...[...(stream?.steps.values() || [])].map(item => element('li', stepLabel(item))));
}

async function loadMetrics(id, version) {
  const metrics = await api('/runs/' + encodeURIComponent(id) + '/metrics');
  if (version !== selection || selectedRun() || current?.runs[0]?.id !== id) return;
  const steps = metrics.steps.map(step => element('li', stepLabel(step)));
  $('progress-steps').replaceChildren(...(steps.length ? steps : [element('li', '这一轮没有留下工具调用记录。')]));
}

function parseEvent(block) {
  let kind = '', id = 0;
  const payload = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) kind = line.slice(7);
    if (line.startsWith('id: ')) id = Number(line.slice(4));
    if (line.startsWith('data: ')) payload.push(line.slice(6));
  }
  return payload.length ? {kind, id, data: JSON.parse(payload.join('\n'))} : null;
}

async function readEvents(stream) {
  const token = localStorage.getItem(TOKEN);
  const response = await fetch(`/api/v2/agent/runs/${encodeURIComponent(stream.id)}/events?after=${stream.cursor}`, {
    headers: {Authorization: `Bearer ${token}`},
    signal: AbortSignal.any([stream.controller.signal, AbortSignal.timeout(45000)]), cache: 'no-store',
  });
  if (response.status === 401 && stream.epoch === authEpoch && !stream.controller.signal.aborted) {
    clearAccount(token);
    notify('登录已失效，请重新登录。');
    return false;
  }
  if (!response.ok || !response.body) throw new Error('进度连接中断');
  stream.connected = true;
  renderProgress(stream);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (watcher === stream) {
      const {value, done} = await reader.read();
      if (done) return false;
      buffer += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf('\n\n')) !== -1 && watcher === stream) {
        const event = parseEvent(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (!event) continue;
        stream.cursor = event.id || stream.cursor;
        if (event.kind === 'result') return true;
        if (event.kind === 'queued' || event.kind === 'running') activeRun = {...activeRun, status: event.kind};
        if (event.kind === 'cancelling') activeRun = {...activeRun, cancel_requested: true};
        if (event.kind === 'progress') stream.steps.set(event.data.step_id || event.id, event.data);
        renderProgress(stream);
        renderControls();
      }
    }
    return false;
  } finally {
    await reader.cancel().catch(() => {});
  }
}

async function watch(stream) {
  let delay = 800;
  while (watcher === stream) {
    let done = false;
    try {
      done = await readEvents(stream);
      delay = 800;
    } catch {
      if (watcher !== stream) return;
      stream.connected = false;
      renderProgress(stream);
    }
    if (watcher !== stream) return;
    if (!done) {
      try {
        const run = await api('/runs/' + encodeURIComponent(stream.id));
        if (watcher !== stream) return;
        done = TERMINAL.has(run.status);
        if (!done) setActive(run);
      } catch (error) {
        if (watcher !== stream) return;
        stream.connected = false;
        renderProgress(stream);
      }
    }
    if (done) {
      setActive(null);
      const results = await Promise.allSettled([loadSessions(), refreshSession(), loadKnowledge()]);
      if (stream.epoch === authEpoch && results.some(result => result.status === 'rejected')) {
        notify('研究已结束，部分内容暂未加载。请重新打开对话查看。');
      }
      return;
    }
    await new Promise(resolve => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 8000);
  }
}

async function cancelRun() {
  const run = selectedRun();
  if (!run || cancelling) return;
  const epoch = authEpoch;
  cancelling = true;
  renderControls();
  try {
    const result = await api('/runs/' + encodeURIComponent(run.id) + '/cancel', {method: 'POST'});
    if (activeRun?.id === run.id && TERMINAL.has(result.status)) {
      setActive(null);
      await Promise.all([loadSessions(), refreshSession(), loadKnowledge()]);
    } else if (!TERMINAL.has(result.status)) setActive(result);
  } finally {
    if (epoch === authEpoch) {
      cancelling = false;
      renderControls();
    }
  }
}

function artifactTitle(artifact) {
  if (artifact.rows) return `志愿方案 · 第 ${artifact.plan_version || 1} 版`;
  if (artifact.role === 'reviewer') return '方案审核';
  return (ROLES[artifact.role] || '研究') + '报告';
}

function artifactSummary(artifact) {
  if (artifact.role === 'reviewer') {
    return {pass: '审核通过，查看仍待核实的事项', revise: '需要修正', insufficient_data: '资料不足'}[artifact.verdict] || '查看审核结果';
  }
  return artifact.findings || artifact.strategy || '';
}

function renderArtifacts(state) {
  const artifacts = Object.values(state?.artifacts || {}).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  $('artifact-count').textContent = `${artifacts.length} 份`;
  $('artifacts').replaceChildren();
  if (!artifacts.length) $('artifacts').append(element('p', '研究报告、审核和方案会出现在这里。', 'empty'));
  for (const artifact of artifacts) {
    const card = button('', () => showArtifact(artifact, state), 'artifact-card');
    card.append(element('strong', artifactTitle(artifact)), element('span', artifactSummary(artifact).slice(0, 88)));
    if (usesUnverifiedSources(artifact, state)) card.append(element('span', '含待核实网页摘要', 'artifact-tag'));
    if (artifact.profile_version !== state.profile_version) card.append(element('span', '历史条件版本', 'artifact-tag'));
    $('artifacts').append(card);
  }
}

function makeTable(headings, rows, caption) {
  const wrap = element('div', null, 'table-scroll');
  const table = element('table');
  if (caption) table.append(element('caption', caption, 'sr-only'));
  const header = table.createTHead().insertRow();
  for (const value of headings) {
    const cell = element('th', value);
    cell.scope = 'col';
    header.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const value of row) line.insertCell().textContent = value ?? '未提供';
  }
  wrap.append(table);
  return wrap;
}

function historyTable(rows) {
  return makeTable(['年份', '最低位次', '最低分', '录取人数'],
    rows.map(row => [row.year, row.min_rank, row.min_score, row.enrollment_count]), '历史录取记录');
}

function usesUnverifiedSources(artifact, state) {
  return (artifact.source_ids || []).some(id => state.sources[id]?.verification === 'unverified');
}

function showArtifact(artifact, state) {
  const title = artifactTitle(artifact);
  currentArtifact = {artifact, title};
  $('artifact-title').textContent = title;
  const content = $('artifact-content');
  content.replaceChildren();
  content.append(element('p', [
    artifact.target_year && `${artifact.target_year} 年`,
    `考生条件第 ${artifact.profile_version} 版`,
    artifact.profile_version !== state.profile_version && '历史版本',
  ].filter(Boolean).join(' · '), 'subtle'));
  if (usesUnverifiedSources(artifact, state)) {
    content.append(element('p', '本成果引用待核实的网页摘要。适用年度、专业组、计划、选科和费用以正式原文为准。', 'subtle'));
  }
  appendAnswer(content, artifactSummary(artifact));
  const policySources = new Map();
  if (artifact.rows) {
    content.append(element('p', '研究草案。请核实目标年度的正式招生计划、专业组及报考资格。', 'subtle'));
    for (const [label, rows] of [['有序候选', artifact.rows], ['条件不符', artifact.excluded || []]]) {
      if (!rows.length) continue;
      content.append(element('h3', `${label}（${rows.length}）`));
      content.append(makeTable(['院校和专业', '招生类型', '城市与资格', '推荐理由', '取舍与待核实'],
        rows.map(row => [
          [row.university_name, row.major_name].filter(Boolean).join('\n'),
          row.enrollment_type,
          [row.city, ELIGIBILITY[row.eligibility] || row.eligibility,
            row.qualification?.group_code && '专业组 ' + row.qualification.group_code].filter(Boolean).join('\n'),
          row.reason,
          [row.tradeoffs, ...(row.differences || []), ...(row.unverified || [])].filter(Boolean).join('；'),
        ]), label));
      for (const row of rows) {
        for (const policy of row.qualification?.sources || []) policySources.set(policy.policy_id, policy);
      }
    }
  }
  for (const issue of artifact.issues || []) {
    content.append(element('p', `${issue.severity === 'blocking' ? '需修正' : '待核实'}：${issue.description}\n建议：${issue.requested_action}`, 'pre-line'));
  }
  if (artifact.missing?.length) content.append(element('p', '尚需核实：' + artifact.missing.join('；')));
  const sources = (artifact.source_ids || []).map(id => state.sources[id]).filter(Boolean);
  if (sources.length) content.append(element('h3', `资料来源（${sources.length}）`));
  for (const source of sources) {
    const card = element('section', null, 'source-card');
    card.append(element('h4', source.title || [source.university_name, source.major_name, '历史录取'].filter(Boolean).join(' · ')));
    if (source.source_id) card.append(element('p', source.source_id, 'subtle source-id'));
    const href = source.url && safeLink(source.url);
    if (href) {
      const anchor = element('a', href);
      anchor.href = href;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      card.append(anchor, element('p', source.authority === 'official_domain' ? '官方域名搜索摘要，原文待核实' : '补充来源摘要，待核实', 'subtle'));
    }
    if (source.text) card.append(element('p', source.text, 'pre-line'));
    if (source.admission_history?.length) card.append(historyTable(source.admission_history));
    content.append(card);
  }
  if (policySources.size) {
    content.append(element('h3', '目标年度招生规则'));
    for (const policy of policySources.values()) {
      const href = safeLink(policy.url);
      const line = element('p');
      const title = policy.title || policy.policy_id;
      if (href) {
        const link = element('a', title);
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        line.append(link, document.createTextNode(' · ' + href));
      } else line.textContent = title;
      content.append(line);
    }
  }
  if (!selectedSession()?.archived) {
    content.append(button('围绕这份成果继续', () => {
      const prompt = artifact.profile_version === state.profile_version
        ? `请基于「${title}」（${artifact.artifact_id}）继续分析，先列出需要核实的问题。`
        : `请参考历史成果「${title}」（${artifact.artifact_id}），按我现在的条件重新核实。`;
      if (!appendDraft(prompt)) return;
      $('artifact-dialog').close();
      $('message').focus();
    }, 'secondary'));
  }
  $('artifact-dialog').showModal();
}

function appendDraft(prompt) {
  const draft = [$('message').value.trimEnd(), prompt].filter(Boolean).join('\n');
  if (draft.length > $('message').maxLength) {
    const dialog = document.querySelector('dialog[open]');
    if (dialog) {
      let feedback = dialog.querySelector('.draft-error');
      if (!feedback) {
        feedback = element('p', null, 'draft-error form-error');
        feedback.setAttribute('role', 'alert');
        dialog.querySelector('.dialog-body').append(feedback);
      }
      feedback.textContent = '草稿空间不足，请先精简输入框中的文字。';
      feedback.scrollIntoView({block: 'nearest'});
    } else notify('草稿空间不足，请先精简输入框中的文字。');
    return false;
  }
  setDraft(draft);
  return true;
}

function exportArtifact() {
  if (!currentArtifact) return;
  const {title} = currentArtifact;
  const content = $('artifact-content').cloneNode(true);
  for (const control of content.querySelectorAll('button, .draft-error')) control.remove();
  for (const table of content.querySelectorAll('table')) {
    const rows = [...table.rows].map(row => [...row.cells].map(cell => cell.textContent.replace(/[\r\n]+/g, ' / ')).join('\t'));
    table.replaceWith(element('p', rows.join('\n'), 'pre-line'));
  }
  const text = [title, ...[...content.children].map(item => item.innerText || item.textContent)].join('\n\n');
  const link = element('a');
  const url = URL.createObjectURL(new Blob([text], {type: 'text/plain;charset=utf-8'}));
  link.href = url;
  link.download = title.replace(/[^\p{L}\p{N}]+/gu, '-') + '.txt';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function copy(value) {
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(value);
  } else {
    const selection = document.createElement('textarea');
    selection.value = value;
    selection.className = 'sr-only';
    (document.querySelector('dialog[open]') || document.body).append(selection);
    selection.select();
    const copied = document.execCommand('copy');
    selection.remove();
    if (!copied) throw new Error('无法复制，请手动选中内容。');
  }
  notify('已复制。');
}

function profileSummary(profile) {
  const basic = profile?.basic_info;
  if (!basic?.province) return '';
  return [basic.province, basic.subject_type, basic.target_year && `${basic.target_year} 年`,
    basic.province_rank && `省位次 ${basic.province_rank.toLocaleString('zh-CN')}`,
    basic.gaokao_score != null && `${basic.gaokao_score} 分`].filter(Boolean).join(' · ');
}

function renderProfileSummary() {
  const summary = profileSummary(knowledge.profile);
  const imported = current?.state.uses_account_profile === false;
  feedback('session-context', imported ? `此对话使用导入的画像 · ${profileSummary(current.state.profile)}` : '');
  $('profile-summary').textContent = summary || '可以先聊专业。保存考生条件后，可以对照省位次和选科。';
  const basic = (imported ? current.state.profile : knowledge.profile)?.basic_info;
  const scope = basic?.province ? coverage.filter(item => item.province === basic.province && item.subject_type === basic.subject_type) : coverage;
  const regions = [...new Set(scope.map(item => item.province))];
  const years = [...new Set(scope.flatMap(item => item.years))].sort((a, b) => a - b);
  feedback('coverage', years.length ? `历年录取库 · ${regions.join('、')} · ${years.join('、')}` :
    basic?.province ? '录取库暂无该省份与科类的记录。' : '录取库暂无已导入的记录。');
  $('memory-summary').textContent = knowledge.memories.length ? `${knowledge.memories.length} 条偏好，可更正或删除。` : '暂无已记录的偏好。';
}

async function loadKnowledge() {
  const index = ++knowledgeRequest;
  const data = await api('/knowledge');
  if (index !== knowledgeRequest) return;
  knowledge = data;
  renderProfileSummary();
  if ($('memory-dialog').open) renderMemories();
}

async function loadCoverage() {
  const data = await api('/coverage');
  coverage = data.admissions;
  $('province-list').replaceChildren(...[...new Set(coverage.map(item => item.province))].map(region => {
    const option = element('option');
    option.value = region;
    return option;
  }));
  renderProfileSummary();
}

function defaultYear() {
  const fields = new Intl.DateTimeFormat('en', {timeZone: 'Asia/Shanghai', month: 'numeric', year: 'numeric'})
    .formatToParts(new Date());
  const month = Number(fields.find(field => field.type === 'month').value);
  const year = Number(fields.find(field => field.type === 'year').value);
  return year + (month >= 9 ? 1 : 0);
}

function openProfile() {
  const {basic_info: basic = {}, constraints = {}} = knowledge.profile;
  const fields = $('profile-form').elements;
  for (const name of ['province', 'subject_type', 'province_rank', 'gaokao_score']) fields[name].value = basic[name] ?? '';
  fields.target_year.value = basic.target_year ?? defaultYear();
  fields.annual_budget_max.value = constraints.annual_budget_max ?? '';
  fields.preferred_cities.value = (constraints.preferred_cities || []).join('、');
  fields.excluded_cities.value = (constraints.excluded_cities || []).join('、');
  fields.accept_sino_foreign.checked = !!constraints.accept_sino_foreign;
  for (const check of $('subjects').querySelectorAll('input')) {
    check.checked = (basic.selected_subjects || []).some(subject => subject.replace('思想政治', '政治') === check.value);
  }
  feedback('profile-error');
  $('profile-dialog').showModal();
}

async function saveProfile(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = form.elements;
  const submit = form.querySelector('[type=submit]');
  if (submit.disabled) return;
  const subjects = [...$('subjects').querySelectorAll('input:checked')].map(input => input.value);
  const split = value => [...new Set(value.split(/[,，、；;]/).map(item => item.trim()).filter(Boolean))];
  const number = input => input.value === '' ? null : input.valueAsNumber;
  feedback('profile-error');
  submit.disabled = true;
  try {
    await api('/knowledge/profile', {method: 'PUT', body: {
      basic_info: {province: fields.province.value, subject_type: fields.subject_type.value,
        target_year: number(fields.target_year), province_rank: number(fields.province_rank),
        gaokao_score: number(fields.gaokao_score), selected_subjects: subjects},
      constraints: {annual_budget_max: number(fields.annual_budget_max),
        preferred_cities: split(fields.preferred_cities.value), excluded_cities: split(fields.excluded_cities.value),
        accept_sino_foreign: fields.accept_sino_foreign.checked},
    }});
    $('profile-dialog').close();
    notify('考生条件已保存。');
    await Promise.all([loadKnowledge(), refreshSession()]);
  } catch (error) {
    if (error.name !== 'AbortError') {
      if ($('profile-dialog').open) feedback('profile-error', error.message);
      else handleError(error);
    }
  } finally {
    submit.disabled = false;
  }
}

function renderMemories() {
  const container = $('memories');
  container.replaceChildren();
  if (!knowledge.memories.length) container.append(element('p', '暂无已记录的偏好。', 'empty'));
  for (const memory of knowledge.memories) {
    const name = FIELDS[memory.field] || memory.field;
    const card = element('form', null, 'memory-card');
    const field = element('label', name);
    const input = element('input');
    input.value = memory.value;
    input.required = true;
    input.maxLength = 300;
    field.append(input);
    const actions = element('div', null, 'actions');
    const save = element('button', '保存更正', 'secondary');
    save.type = 'submit';
    const error = element('p', '', 'form-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    let updating = false;
    async function update(remove) {
      if (updating || activeRun) return;
      updating = true;
      save.disabled = discard.disabled = true;
      error.hidden = true;
      try {
        const options = remove ? {method: 'DELETE'} : {method: 'PATCH', body: {value: input.value}};
        await api('/knowledge/memories/' + encodeURIComponent(memory.id), options);
        await Promise.all([loadKnowledge(), refreshSession()]);
      } catch (exception) {
        if (exception.name !== 'AbortError') {
          error.textContent = exception.message;
          error.hidden = false;
        }
      } finally {
        updating = false;
        save.disabled = discard.disabled = !!activeRun;
      }
    }
    const discard = button('删除', () => update(true));
    card.addEventListener('submit', event => { event.preventDefault(); update(false); });
    actions.append(save, discard);
    card.append(field, error, actions);
    container.append(card);
  }
  renderControls();
}

function openSessionSettings() {
  const session = selectedSession();
  if (!session) return;
  $('session-title').value = session.title || '新对话';
  $('archive-session').textContent = session.archived ? '恢复对话' : '归档';
  $('archive-session').disabled = !!selectedRun();
  feedback('session-error');
  $('session-dialog').showModal();
}

async function editSession(body) {
  const id = sid;
  const epoch = authEpoch;
  const form = $('session-form');
  const controls = [...form.querySelectorAll('button')];
  controls.forEach(control => control.disabled = true);
  try {
    await api(sessionPath(id), {method: 'PATCH', body});
    $('session-dialog').close();
    if (typeof body.archived === 'boolean' && sid === id) showingArchive = body.archived;
    await loadSessions();
    renderConversation();
    notify(body.archived === true ? '对话已归档。' : body.archived === false ? '对话已恢复。' : '标题已保存。');
  } catch (error) {
    if (epoch === authEpoch && $('session-dialog').open) feedback('session-error', error.message);
    else handleError(error);
  } finally {
    controls.forEach(control => control.disabled = false);
  }
}

function openHistory() {
  const basic = current?.state.profile?.basic_info || knowledge.profile.basic_info || {};
  const fields = $('history-form').elements;
  if (!fields.province.value) fields.province.value = basic.province || coverage[0]?.province || '广东';
  if (!historyQuery && basic.subject_type) fields.subject_type.value = basic.subject_type;
  if (!historyQuery && basic.province === fields.province.value && basic.subject_type === fields.subject_type.value) {
    fields.near_rank.value = basic.province_rank ?? '';
  }
  if (!fields.target_year.value) fields.target_year.value = basic.target_year ?? defaultYear();
  $('history-dialog').querySelector('.draft-error')?.remove();
  $('history-dialog').showModal();
}

function renderComparison() {
  const items = [...comparison.values()];
  $('history-comparison').hidden = items.length === 0;
  $('comparison-count').textContent = `对照候选（${items.length}/3）`;
  const years = [...new Set(items.flatMap(item => item.admission_history.map(row => row.year)))].sort((a, b) => b - a);
  const titles = items.map(item => `${item.university_name} · ${item.major_name}\n${item.province} · ${item.subject_type} · ${item.enrollment_type}`);
  const values = years.map(year => [year, ...items.map(item => {
    const row = item.admission_history.find(entry => entry.year === year);
    return row ? `位次 ${row.min_rank ?? '未提供'} / 分数 ${row.min_score ?? '未提供'}` : '无该年记录';
  })]);
  const container = $('comparison-table');
  container.replaceChildren();
  if (items.length) container.append(makeTable(['年份', ...titles], values, '历史录取候选对照'));
  for (const control of $('history-results').querySelectorAll('[data-candidate]')) {
    const checked = comparison.has(control.dataset.candidate);
    control.setAttribute('aria-pressed', String(checked));
    control.textContent = checked ? '移出对照' : '加入对照';
    control.disabled = !checked && items.length >= 3;
  }
}

async function searchHistory(params, offset = 0) {
  const index = ++historyRequest;
  const submit = $('history-form').querySelector('[type=submit]');
  const body = $('history-results');
  const query = new URLSearchParams(Object.entries({...params, offset: String(offset)}).filter(([, value]) => value !== ''));
  $('history-prev').hidden = $('history-next').hidden = true;
  submit.disabled = true;
  body.replaceChildren();
  $('history-status').textContent = '正在查询历史录取…';
  try {
    const result = await api('/history?' + query);
    if (index !== historyRequest) return;
    historyQuery = {params, offset, next: result.next_offset};
    const rankBasis = result.rank_year ? ` · 按 ${result.rank_year} 年记录与输入位次的差值排序` : '';
    $('history-status').textContent = !result.covered_years.length ? '录取库暂无该省份与科类的数据。' :
      params.near_rank && !result.rank_year ? '目标年份之前暂无可用于对照的位次记录。' :
      !result.total ? '未找到匹配记录，请调整关键词或清空对照省位次。' :
      `共 ${result.total} 条候选 · 显示 ${offset + 1}–${offset + result.returned} · 覆盖 ${result.covered_years.join('、')} 年${rankBasis}`;
    for (const candidate of result.candidates) {
      const card = element('section', null, 'source-card');
      const name = `${candidate.university_name} · ${candidate.major_name}`;
      card.append(element('h3', name), element('p', [candidate.city, candidate.subject_type, candidate.enrollment_type].filter(Boolean).join(' · '), 'subtle'));
      const rank = candidate.admission_history.find(row => row.year === result.rank_year)?.min_rank;
      if (rank) card.append(element('p', `${result.rank_year} 年最低位次与输入位次相差 ${Math.abs(rank - Number(params.near_rank)).toLocaleString('zh-CN')} 名`, 'subtle'));
      card.append(historyTable(candidate.admission_history));
      function attachHistory(plan) {
        const label = `${candidate.university_name}的${candidate.major_name}（${candidate.subject_type}，${candidate.enrollment_type}）`;
        const prompt = plan ? `请为${label}拟一份带历史来源的志愿研究草案，先请求我确认调查。` :
          `请研究${label}，帮我核对历史位次与目标年度的报考条件。`;
        if (!appendDraft(prompt)) return;
        localStorage.setItem(key('history_candidate'), JSON.stringify({id: candidate.candidate_id, prompt, plan}));
        renderControls();
        $('history-dialog').close();
        $('message').focus();
      }
      const ask = button('放入对话', () => attachHistory(false), 'secondary');
      const plan = button('拟研究草案', () => attachHistory(true), 'secondary');
      ask.disabled = plan.disabled = !!selectedSession()?.archived;
      const candidateKey = String(candidate.candidate_id);
      const compare = button('加入对照', () => {
        if (comparison.has(candidateKey)) comparison.delete(candidateKey);
        else if (comparison.size < 3) comparison.set(candidateKey, candidate);
        renderComparison();
        if (comparison.size) $('history-comparison').open = true;
      });
      compare.dataset.candidate = candidateKey;
      const actions = element('div', null, 'actions');
      actions.append(ask, plan, compare);
      card.append(actions);
      body.append(card);
    }
    renderComparison();
    $('history-prev').hidden = offset === 0;
    $('history-next').hidden = result.next_offset == null;
  } catch (error) {
    if (index === historyRequest && error.name !== 'AbortError') $('history-status').textContent = error.message;
  } finally {
    if (index === historyRequest) submit.disabled = false;
  }
}

function closePanel(id) {
  $(id).classList.remove('mobile-open');
  $(id === 'sidebar' ? 'toggle-sessions' : 'toggle-details').setAttribute('aria-expanded', 'false');
}

function togglePanel(id, control) {
  const isOpen = $(id).classList.toggle('mobile-open');
  $(control).setAttribute('aria-expanded', String(isOpen));
  if (isOpen) closePanel(id === 'sidebar' ? 'inspector' : 'sidebar');
}

async function start() {
  const token = localStorage.getItem(TOKEN);
  if (!token) {
    clearAccount();
    return;
  }
  const epoch = ++authEpoch;
  $('loading').hidden = false;
  $('workspace').hidden = $('auth').hidden = true;
  $('retry-start').hidden = true;
  $('loading-text').textContent = '正在连接服务…';
  try {
    const me = await request('/api/v1/auth/me');
    account = me.data.user;
    finishedRuns.clear();
    $('account-name').textContent = account.username;
    const query = new URL(location.href).searchParams;
    await Promise.all([loadSessions(), loadKnowledge(), loadCoverage()]);
    let requested = query.get('session');
    const profile = query.get('profile');
    if (profile && !requested) {
      const importKey = key('import', profile);
      const request_id = localStorage.getItem(importKey) || requestId();
      localStorage.setItem(importKey, request_id);
      try {
        const result = await api('/sessions', {method: 'POST', body: {profile_uuid: profile, request_id}});
        requested = result.id;
        changeUrl(requested, true, true);
        localStorage.removeItem(importKey);
        await loadSessions();
      } catch (error) {
        if (error.status !== 404) throw error;
        changeUrl(null, true, true);
        localStorage.removeItem(importKey);
        notify('当前账号无法打开该画像。');
      }
    }
    const saved = localStorage.getItem(key('session', null));
    const exists = id => sessions.some(item => item.id === id);
    if (requested && !exists(requested)) notify('当前账号无法打开该对话。');
    const preferred = exists(requested) ? requested : exists(saved) ? saved :
      saved === '' ? null : activeRun?.session_id || sessions.find(item => !item.archived)?.id || null;
    await selectSession(preferred, {replace: true});
    $('loading').hidden = true;
    $('workspace').hidden = $('logout').hidden = $('account-name').hidden = false;
  } catch (error) {
    if (epoch !== authEpoch) {
      if (error.message === '登录已失效，请重新登录。') notify(error.message);
      return;
    }
    $('loading-text').textContent = error.message;
    $('retry-start').hidden = false;
  }
}

for (const name of SUBJECTS) {
  const label = element('label', null, 'check');
  const check = element('input');
  check.type = 'checkbox';
  check.value = name;
  label.append(check, document.createTextNode(name));
  $('subjects').append(label);
}

document.querySelectorAll('[data-close]').forEach(control => {
  control.addEventListener('click', () => control.closest('dialog').close());
});
$('auth-form').addEventListener('submit', authenticate);
$('auth-switch').addEventListener('click', () => setAuthMode(authMode === 'login' ? 'register' : 'login'));
$('logout').addEventListener('click', () => {
  clearAccount(localStorage.getItem(TOKEN), true);
  notify('已退出登录。');
  $('username').focus();
});
$('retry-start').addEventListener('click', start);
$('new-session').addEventListener('click', () => {
  showingArchive = false;
  selectSession(null, {focus: true}).catch(handleError);
});
$('show-archive').addEventListener('click', () => { showingArchive = !showingArchive; renderSessions(); });
$('session-search').addEventListener('input', renderSessions);
$('session-settings').addEventListener('click', openSessionSettings);
$('session-form').addEventListener('submit', event => { event.preventDefault(); editSession({title: $('session-title').value}); });
$('copy-link').addEventListener('click', () => copy(location.href).catch(error => feedback('session-error', error.message)));
$('archive-session').addEventListener('click', () => editSession({archived: !selectedSession()?.archived}));
$('restore-session').addEventListener('click', () => editSession({archived: false}));
$('toggle-sessions').addEventListener('click', () => togglePanel('sidebar', 'toggle-sessions'));
$('toggle-details').addEventListener('click', () => togglePanel('inspector', 'toggle-details'));
$('go-to-run').addEventListener('click', () => selectSession(activeRun?.session_id || null).catch(handleError));
$('message').addEventListener('input', () => setDraft($('message').value));
$('message').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  if (matchMedia('(pointer: coarse)').matches && !event.ctrlKey && !event.metaKey) return;
  event.preventDefault();
  if (!$('send').disabled) $('message-form').requestSubmit();
});
$('message-form').addEventListener('submit', event => {
  event.preventDefault();
  send($('message').value).catch(handleError);
});
$('cancel').addEventListener('click', () => cancelRun().catch(handleError));
$('edit-profile').addEventListener('click', openProfile);
$('profile-form').addEventListener('submit', saveProfile);
$('open-memory').addEventListener('click', () => { renderMemories(); $('memory-dialog').showModal(); });
$('export-artifact').addEventListener('click', exportArtifact);
for (const id of ['history-open', 'mobile-history']) $(id).addEventListener('click', openHistory);
$('history-form').addEventListener('submit', event => {
  event.preventDefault();
  searchHistory(Object.fromEntries(new FormData(event.currentTarget)));
});
for (const name of ['province', 'subject_type']) {
  $('history-form').elements[name].addEventListener('change', () => {
    $('history-form').elements.near_rank.value = '';
  });
}
$('history-prev').addEventListener('click', () => searchHistory(historyQuery.params, Math.max(0, historyQuery.offset - 10)));
$('history-next').addEventListener('click', () => searchHistory(historyQuery.params, historyQuery.next));
$('clear-comparison').addEventListener('click', () => { comparison.clear(); renderComparison(); });
window.addEventListener('popstate', () => {
  if (!account) return;
  const id = new URL(location.href).searchParams.get('session');
  selectSession(sessions.some(session => session.id === id) ? id : null, {writeUrl: false}).catch(handleError);
});
window.addEventListener('storage', event => {
  if (event.key === TOKEN && event.oldValue !== event.newValue) {
    clearAccount(undefined, !!event.oldValue);
    start();
  }
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && account && !$('workspace').hidden) {
    Promise.all([loadSessions(), refreshSession()]).catch(handleError);
  }
});

start();
