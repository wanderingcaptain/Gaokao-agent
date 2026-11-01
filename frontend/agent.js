/* Persistent planning task UI. Server owns conversation and plan versions. */
(() => {
  let taskId = sessionStorage.getItem('gaokao_agent_task');
  let snapshot = null;
  let busy = false;
  let pollTimer = null;
  const el = id => document.getElementById(id);
  const split = value => value.split(/[,，、]/).map(s => s.trim()).filter(Boolean);
  async function api(path, options = {}) {
    const response = await fetch('/api/v1/agent' + path, {...options, headers: authHeaders()});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查画像条件');
    }
    return response;
  }
  function status(message) {
    el('ai-banner').classList.remove('done');
    setBannerMsg(message);
  }
  function displayPlan(plan) {
    window.gaokaoAgentContext = {task_id: taskId, profile_version: plan.profile_version};
    candidates = plan.candidates || [];
    aiDone = true;
    aiStopped = false;
    isFallbackResult = !!plan.fallback;
    sessionStorage.setItem('gaokao_candidates', JSON.stringify(candidates));
    sessionStorage.setItem('gaokao_ai_done', 'true');
    sessionStorage.setItem('gaokao_ai_fallback', String(isFallbackResult));
    if (plan.result_id) sessionStorage.setItem('gaokao_result_id', String(plan.result_id));
    updateCounts(); renderList();
    el('detail-panel').classList.add('hidden');
    el('detail-empty').classList.remove('hidden');
    status(plan.message || '方案已更新');
  }
  function fillEditor(profile) {
    const c = profile.constraints || {}, a = profile.academic_preferences || {};
    el('agent-cities').value = (c.preferred_cities || []).join('，');
    el('agent-excluded').value = (c.excluded_cities || []).join('，');
    el('agent-majors').value = (a.liked_subjects || []).join('，');
    el('agent-disliked').value = (a.disliked_subjects || []).join('，');
    el('agent-budget').value = c.annual_budget_max ?? 150000;
    el('agent-cooperation').checked = !!c.accept_sino_foreign;
  }
  function editedProfile() {
    const p = snapshot.state.profiles.at(-1);
    return {...p.basic_info, ...p.academic_preferences, ...p.constraints,
      personality_traits: p.personality?.traits || [],
      career_vision: p.personality?.career_vision || '',
      priority_factors: p.priority_factors || [], preference_tree: p.preference_tree || [],
      extra_notes: p.extra_notes || '',
      preferred_cities: split(el('agent-cities').value),
      excluded_cities: split(el('agent-excluded').value),
      liked_subjects: split(el('agent-majors').value),
      disliked_subjects: split(el('agent-disliked').value),
      annual_budget_max: Number(el('agent-budget').value),
      accept_sino_foreign: el('agent-cooperation').checked};
  }
  async function refresh() {
    snapshot = await (await api('/tasks/' + taskId)).json();
    busy = snapshot.status === 'running';
    el('agent-send').disabled = busy;
    el('agent-cancel').disabled = !busy;
    const box = el('agent-conversation');
    box.replaceChildren();
    for (const message of snapshot.state.conversation) {
      const row = document.createElement('p');
      row.textContent = (message.role === 'user' ? '你：' : '助手：') + message.content;
      box.appendChild(row);
    }
    box.scrollTop = box.scrollHeight;
    const select = el('agent-versions');
    const selected = select.value;
    select.replaceChildren();
    for (const plan of snapshot.state.plans) {
      const option = document.createElement('option');
      option.value = plan.version;
      option.textContent = '版本 ' + plan.version + (plan.fallback ? '（规则备选）' : '');
      select.appendChild(option);
    }
    if (selected && snapshot.state.plans.some(p => String(p.version) === selected)) select.value = selected;
    else if (snapshot.state.plans.length) {
      select.value = snapshot.state.plans.at(-1).version;
      displayPlan(snapshot.state.plans.at(-1));
    }
    if (!el('agent-edit').checked) fillEditor(snapshot.state.profiles.at(-1));
    if (busy) {
      status('本轮正在处理，刷新页面后仍可查看进度');
      clearTimeout(pollTimer);
      pollTimer = setTimeout(() => refresh().catch(e => status(e.message)), 1500);
    } else if (snapshot.status === 'waiting') {
      status('请回答助手的问题；需要修改限制时勾选“应用以下修改”');
    } else if (['cancelled', 'failed'].includes(snapshot.status)) {
      status(snapshot.status === 'cancelled' ? '本轮已取消，可以发送新的要求' : '本轮中断，可以发送消息继续');
    }
  }
  async function send(content) {
    if (busy) return;
    busy = true;
    el('agent-send').disabled = true;
    el('agent-cancel').disabled = false;
    const body = {request_id: crypto.randomUUID(), content};
    if (el('agent-edit').checked) body.profile = editedProfile();
    try {
      const response = await api('/tasks/' + taskId + '/messages', {
        method: 'POST', body: JSON.stringify(body)});
      el('agent-edit').checked = false;
      el('agent-input').value = '';
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          const lines = frame.split('\n');
          const kind = lines.find(s => s.startsWith('event: '))?.slice(7);
          const raw = lines.find(s => s.startsWith('data: '))?.slice(6);
          if (!raw) continue;
          const data = JSON.parse(raw);
          if (kind === 'progress') status(data.message);
          if (kind === 'result') {
            displayPlan(data);
            el('agent-versions').value = '';
          }
          if (kind === 'waiting') status(data.question);
          if (kind === 'answer') status('比较完成，见对话记录');
          if (kind === 'error') status(data.message);
        }
      }
    } catch (error) {
      status(error.message);
    } finally {
      busy = false;
      await refresh().catch(e => status(e.message));
    }
  }
  window.cancelAgentTurn = async () => {
    if (!taskId) return;
    try { await api('/tasks/' + taskId + '/cancel', {method: 'POST'}); await refresh(); }
    catch (error) { status(error.message); }
  };
  el('agent-cancel').addEventListener('click', window.cancelAgentTurn);
  el('agent-form').addEventListener('submit', e => {
    e.preventDefault();
    const value = el('agent-input').value.trim();
    if (value) send(value);
  });
  el('agent-versions').addEventListener('change', () => {
    const plan = snapshot?.state.plans.find(p => String(p.version) === el('agent-versions').value);
    if (plan) displayPlan(plan);
  });
  async function boot() {
    if (!getToken() || !uuid) return;
    if (taskId) {
      await refresh();
      if (snapshot.status !== 'ready') return;
    } else {
      const result = await (await api('/tasks', {method: 'POST', body: JSON.stringify({
        profile_uuid: uuid, goal: sessionStorage.getItem('gaokao_agent_goal') || '根据我的画像生成志愿方案'
      })})).json();
      taskId = result.id;
      sessionStorage.setItem('gaokao_agent_task', taskId);
      await refresh();
    }
    await send(snapshot.state.goal);
  }
  boot().catch(e => status(e.message));
})();
