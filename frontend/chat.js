'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const terminal = new Set(['completed','waiting','degraded','failed','cancelled','insufficient_data']);
  const statuses = {queued:'等待执行',running:'研究中',waiting:'等待你的确认',completed:'研究完成',degraded:'本轮已到上限',failed:'本轮未完成',cancelled:'已停止',insufficient_data:'资料不足'};
  const roles = {coordinator:'主对话',major_research:'专业研究',admissions:'招生研究',planner:'方案规划',reviewer:'方案审核'};
  const fields = {interest:'兴趣',career_goal:'职业方向',learning_preference:'学习偏好',excluded_city:'排除城市',annual_budget_max:'年度预算上限'};
  let user, sid, current, activeRun, streamAbort, watching, pendingSubmission;
  const prefix = () => `gaokao_v2_${user.id}_`;
  const token = () => localStorage.getItem('gaokao_auth_token');
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  function notice(text='') { $('notice').textContent = text; $('notice').hidden = !text; }
  async function api(path, options={}) {
    const res = await fetch('/api/v2/agent' + path, {...options, headers:{'Content-Type':'application/json',Authorization:`Bearer ${token()}`, ...options.headers}});
    let data;
    try { data = await res.json(); } catch { throw new Error('服务器返回异常，请稍后重试。'); }
    if (res.status === 401) { logout(); throw new Error('登录已失效，请重新登录。'); }
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查输入的条件或稍后重试。');
    return data;
  }
  function busy(run) {
    activeRun = run && !terminal.has(run.status) ? run : null;
    $('task-status').textContent = run ? (statuses[run.status] || run.status) : '准备就绪';
    $('progress').hidden = !activeRun;
    $('send').disabled = !!activeRun;
    $('cancel').disabled = !activeRun;
    for (const el of $('profile-form').elements) el.disabled = !!activeRun;
    for (const el of $('memories').querySelectorAll('button,input')) el.disabled = !!activeRun;
  }
  function logout() {
    if (streamAbort) streamAbort.abort();
    watching = undefined; activeRun = undefined;
    localStorage.removeItem('gaokao_auth_token'); localStorage.removeItem('gaokao_auth_user');
    $('auth').hidden = false; $('workspace').hidden = true; $('logout').hidden = true;
  }
  async function authenticate(register=false) {
    if (!$('auth-form').reportValidity()) return;
    try {
      const res = await fetch('/api/v1/auth/' + (register ? 'register' : 'login'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('username').value,password:$('password').value})});
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '登录信息不符合要求');
      localStorage.setItem('gaokao_auth_token',data.data.token); localStorage.setItem('gaokao_auth_user',JSON.stringify(data.data.user));
      $('password').value = ''; notice(); await initialize();
    } catch(e) { notice(e.message); }
  }
  async function listSessions() {
    const data = await api('/sessions'); $('sessions').replaceChildren();
    data.sessions.slice().reverse().forEach((s,i) => {
      const b = node('button',(s.running ? '◉ ' : '') + (s.title || `研究对话 ${data.sessions.length-i}`),s.id === sid ? 'active' : '');
      b.type='button'; b.onclick=()=>selectSession(s.id).catch(e=>notice(e.message)); $('sessions').append(b);
    });
    return data.sessions;
  }
  async function newSession() {
    const data = await api('/sessions',{method:'POST',body:'{}'});
    await selectSession(data.id);
  }
  async function selectSession(id) {
    if (streamAbort) streamAbort.abort();
    watching = undefined; sid = id; localStorage.setItem(prefix()+'session',sid);
    await refresh(); await listSessions();
  }
  function showMessages(state) {
    const container = $('messages'); container.replaceChildren();
    if (!state.conversation.length) {
      const welcome=node('div',undefined,'welcome'); welcome.append(node('h2','不用先确定答案，我们可以一起探索。'),node('p','有哪些课、活动或事情，让你愿意主动花时间？暂时没有方向也没关系。','subtle'));
      const suggestions=node('div',undefined,'suggestions');
      ['我还没有明确的专业方向','计算机和电子信息有什么区别？','先聊聊我喜欢做的事情'].forEach(text=>{const b=node('button',text);b.onclick=()=>{$('message').value=text;$('message').focus();};suggestions.append(b);});
      welcome.append(suggestions);container.append(welcome);
    }
    state.conversation.forEach(m=>{const entry=node('div',undefined,'message '+m.role);entry.append(node('div',m.role==='user'?'你':'志愿研究助手','speaker'),node('div',m.content,'body'));container.append(entry);});
    if (activeRun) {
      const entry=node('div',undefined,'message user');entry.append(node('div','你','speaker'),node('div',activeRun.input_text || '已提交研究请求','body'));container.append(entry);
      const hint=node('div','研究在后台进行，刷新或离开后可以回来查看。','subtle');container.append(hint);
    }
    container.scrollTop=container.scrollHeight;
  }
  function showConfirmations(state) {
    $('confirmations').replaceChildren();
    Object.entries(state.pending).forEach(([id,p])=>{
      if (p.profile_version !== state.profile_version) return;
      const card=node('div',undefined,'card');
      const description=p.kind==='research'?p.text:`记录条件：${fields[p.field] || p.field} = ${p.value}`;
      card.append(node('div',description,'card-title'));
      const b=node('button','确认并继续'); b.disabled=!!activeRun;
      b.onclick=()=>send('确认：'+description,[id]).catch(e=>notice(e.message));card.append(b);$('confirmations').append(card);
    });
  }
  function showArtifacts(state) {
    const artifacts=Object.values(state.artifacts).sort((a,b)=>(b.created_at||0)-(a.created_at||0)); $('artifact-count').textContent=`${artifacts.length} 份`;
    $('artifacts').replaceChildren();
    if (!artifacts.length) $('artifacts').append(node('p','研究报告与方案将在这里出现。','empty'));
    for (const a of artifacts) {
      const card=node('div',undefined,'card');const title=a.role==='reviewer'?'审核报告':a.rows?`方案 · 第 ${a.plan_version || 1} 版`:(roles[a.role] || '研究报告');
      card.append(node('div',title,'card-title'),node('p',(a.findings || a.strategy || a.verdict || '').slice(0,85)));
      if(a.profile_version!==state.profile_version)card.append(node('p','历史版本 · 条件已有变化'));
      const button=node('button','查看详情与来源','secondary');button.onclick=()=>showArtifact(a);card.append(button);$('artifacts').append(card);
    }
  }
  function showArtifact(a) {
    $('artifact-title').textContent=a.rows?`志愿研究草案 · 第 ${a.plan_version} 版`:a.role==='reviewer'?'审核报告':`${roles[a.role] || '研究'}报告`;
    const content=$('artifact-content');content.replaceChildren();
    content.append(node('p',a.findings || a.strategy || ''));
    if(a.rows){
      content.append(node('p','研究草案，需继续核实目标年度的正式填报规则。','subtle'));
      for(const [label,rows] of [['有序候选',a.rows],['条件不符，未列入推荐',a.excluded || []]]){
        content.append(node('h2',`${label}（${rows.length}）`));
        const table=node('table'),head=node('tr');['学校 / 专业','城市 / 资格','推荐理由','取舍与待核实'].forEach(s=>head.append(node('th',s)));table.append(head);
        rows.forEach(r=>{const tr=node('tr');[`${r.university_name}\n${r.major_name}`,`${r.city}\n${({eligible:'已核对条件符合',ineligible:'条件不符',unknown:'待核实',conflicting:'资料冲突'})[r.eligibility] || r.eligibility}`,r.reason,[r.tradeoffs,...(r.differences||[]),...(r.unverified||[])].join('；')].forEach(s=>tr.append(node('td',s)));table.append(tr);});content.append(table);
      }
    }
    if(a.issues) a.issues.forEach(i=>content.append(node('p',`${i.severity==='blocking'?'需修正':'待核实'}：${i.description}\n建议：${i.requested_action}`)));
    if(a.missing?.length)content.append(node('p','尚需核实：'+a.missing.join('；')));
    for(const id of a.source_ids || []){
      const source=current.state.sources[id];if(!source)continue;const card=node('div',undefined,'source');card.append(node('strong',source.title || `${source.university_name || ''} ${source.major_name || ''} 历史记录`));
      if(source.url){try{const url=new URL(source.url);if(url.protocol==='https:'){const link=node('a',source.url);link.href=source.url;link.target='_blank';link.rel='noopener noreferrer';card.append(node('br'),link);}}catch{/* Invalid source URLs are displayed only as text. */}}
      if(source.text)card.append(node('p',source.text));
      if(source.admission_history){const table=node('table'),head=node('tr');['年份','最低位次','最低分','录取人数'].forEach(s=>head.append(node('th',s)));table.append(head);
        source.admission_history.forEach(r=>{const tr=node('tr');[r.year,r.min_rank,r.min_score,r.enrollment_count].forEach(v=>tr.append(node('td',v??'未提供')));table.append(tr);});card.append(table);}
      content.append(card);
    }
    $('artifact-dialog').showModal();
  }
  async function refresh() {
    const selected=sid;const data=await api('/sessions/'+selected);if(selected!==sid)return;
    current=data;const latest=data.runs[0];busy(latest);showMessages(data.state);showConfirmations(data.state);showArtifacts(data.state);
    await listSessions();
    if(latest){await showMetrics(latest.id);if(!terminal.has(latest.status)&&watching!==latest.id)watch(latest).catch(e=>notice(e.message));}
  }
  async function showMetrics(id) {
    const data=await api(`/runs/${id}/metrics`);if(current?.runs[0]?.id!==id)return;
    $('metrics').replaceChildren(node('p',`${(data.duration_ms/1000).toFixed(1)} 秒累计调用 · ${data.total_tokens} tokens · ${data.failed_steps} 次步骤错误`));
    data.steps.forEach(s=>$('metrics').append(node('div',`${roles[s.role] || s.role} · ${s.kind==='model'?'模型分析':s.name} · ${s.ok?'完成':'需处理'}`)));
  }
  async function send(text, confirmedIds=[]) {
    if(activeRun)return;
    text=text.trim();if(!text)return;
    notice();$('send').disabled=true;
    const key=prefix()+'pending';let saved;
    try{saved=JSON.parse(localStorage.getItem(key)||'null');}catch{saved=null;}
    const content={text,confirmed_ids:confirmedIds};
    pendingSubmission=saved?.sid===sid&&JSON.stringify(saved.content)===JSON.stringify(content)?saved:{sid,content,request_id:crypto.randomUUID()};
    localStorage.setItem(key,JSON.stringify(pendingSubmission));
    try{
      const run=await api('/sessions/'+sid+'/messages',{method:'POST',body:JSON.stringify({...content,request_id:pendingSubmission.request_id})});
      localStorage.removeItem(key);pendingSubmission=undefined;$('message').value='';busy(run);await refresh();
    }catch(e){$('send').disabled=false;throw e;}
  }
  async function watch(run) {
    if(streamAbort)streamAbort.abort();const controller=new AbortController();streamAbort=controller;watching=run.id;
    const selected=sid;const cursorKey=prefix()+'cursor_'+run.id;let after=Number(localStorage.getItem(cursorKey)||0),attempt=0;
    while(!controller.signal.aborted&&sid===selected){
      try{
        const res=await fetch(`/api/v2/agent/runs/${run.id}/events?after=${after}`,{headers:{Authorization:`Bearer ${token()}`},signal:controller.signal});
        if(!res.ok)throw new Error(res.status===401?'登录已失效，请重新登录。':'进度连接暂时不可用');
        const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';
        while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let cut;
          while((cut=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,cut);buffer=buffer.slice(cut+2);let kind='',data='',id;
            for(const line of block.split('\n')){if(line.startsWith('event: '))kind=line.slice(7);if(line.startsWith('data: '))data+=line.slice(6);if(line.startsWith('id: '))id=Number(line.slice(4));}
            if(!data)continue;const event=JSON.parse(data);if(id){after=id;localStorage.setItem(cursorKey,String(id));}
            if(kind==='running'||kind==='queued')busy({...run,status:event.status});
            if(kind==='cancelling'){$('task-status').textContent='正在停止';$('cancel').disabled=true;}
            if(kind==='progress')$('progress-text').textContent=`${roles[event.role] || '研究'}：${event.kind==='model'?'正在整理分析':'已处理一项资料'}`;
            if(kind==='result'){watching=undefined;controller.abort();await refresh();await loadKnowledge();return;}
          }
        }
        const status=await api(`/runs/${run.id}`);if(terminal.has(status.status)){watching=undefined;await refresh();await loadKnowledge();return;}
        attempt=0;
      }catch(e){if(controller.signal.aborted)return;attempt++;$('progress-text').textContent='连接中断，正在恢复进度…';if(attempt>=8){watching=undefined;notice('暂时无法连接进度。任务仍保留，稍后刷新即可查看。');return;}}
      await new Promise(resolve=>setTimeout(resolve,Math.min(1000*2**attempt,10000)));
    }
  }
  async function loadKnowledge() {
    const data=await api('/knowledge');const profile=data.profile,form=$('profile-form'),basic=profile.basic_info || {},constraints=profile.constraints || {};
    for(const name of ['province','subject_type','province_rank','target_year']) if(basic[name]!==undefined)form.elements[name].value=basic[name];
    form.elements.selected_subjects.value=(basic.selected_subjects || []).join('，');form.elements.excluded_cities.value=(constraints.excluded_cities||[]).join('，');
    form.elements.annual_budget_max.value=constraints.annual_budget_max??'';form.elements.accept_sino_foreign.checked=!!constraints.accept_sino_foreign;
    $('memories').replaceChildren();
    for(const memory of data.memories){const box=node('div',undefined,'memory');box.append(node('div',fields[memory.field] || memory.field));const input=node('input');input.value=memory.value;input.maxLength=300;input.setAttribute('aria-label',fields[memory.field] || memory.field);box.append(input);const actions=node('div',undefined,'actions');
      const save=node('button','保存更正','secondary'),remove=node('button','删除','quiet');save.type=remove.type='button';save.disabled=remove.disabled=!!activeRun;
      save.onclick=async()=>{try{await api('/knowledge/memories/'+encodeURIComponent(memory.id),{method:'PATCH',body:JSON.stringify({value:input.value})});await loadKnowledge();await refresh();notice('偏好已更正。后续研究会使用更新后的条件。');}catch(e){notice(e.message);}};
      remove.onclick=async()=>{try{await api('/knowledge/memories/'+encodeURIComponent(memory.id),{method:'DELETE'});await loadKnowledge();await refresh();notice('已删除这条当前记忆。历史对话和已保存报告仍保留。');}catch(e){notice(e.message);}};actions.append(save,remove);box.append(actions);$('memories').append(box);
    }
  }
  async function initialize() {
    if(!token()){logout();return;}
    const res=await fetch('/api/v1/auth/me',{headers:{Authorization:`Bearer ${token()}`}});if(!res.ok){logout();return;}user=(await res.json()).data.user;
    $('auth').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;
    const sessions=await listSessions();const saved=localStorage.getItem(prefix()+'session');
    if(sessions.length)await selectSession(sessions.some(s=>s.id===saved)?saved:sessions[sessions.length-1].id);else await newSession();
    await loadKnowledge();
  }
  $('auth-form').onsubmit=e=>{e.preventDefault();authenticate();};$('register').onclick=()=>authenticate(true);$('logout').onclick=logout;
  $('new-session').onclick=()=>newSession().catch(e=>notice(e.message));$('message-form').onsubmit=e=>{e.preventDefault();send($('message').value).catch(e=>notice(e.message));};
  $('cancel').onclick=async()=>{try{if(activeRun)await api(`/runs/${activeRun.id}/cancel`,{method:'POST'});await refresh();}catch(e){notice(e.message);}};
  $('close-dialog').onclick=()=>$('artifact-dialog').close();
  $('profile-form').onsubmit=async e=>{e.preventDefault();const f=e.target.elements,split=s=>s.split(/[,，]/).map(x=>x.trim()).filter(Boolean);
    try{await api('/knowledge/profile',{method:'PUT',body:JSON.stringify({basic_info:{province:f.province.value,subject_type:f.subject_type.value,province_rank:Number(f.province_rank.value),target_year:Number(f.target_year.value),selected_subjects:split(f.selected_subjects.value)},constraints:{excluded_cities:split(f.excluded_cities.value),annual_budget_max:f.annual_budget_max.value===''?null:Number(f.annual_budget_max.value),accept_sino_foreign:f.accept_sino_foreign.checked}})});await refresh();notice('条件已保存。新的研究会使用这份画像。');}catch(e){notice(e.message);}};
  initialize().catch(e=>notice(e.message));
})();
