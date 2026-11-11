'use strict';

const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {once} = require('node:events');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '../..');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const live = process.argv.includes('--live');

async function freePort() {
  const server = net.createServer();
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const {port} = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function until(check, description, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await check()) return;
    await sleep(80);
  }
  throw new Error('等待超时：' + description);
}

async function register(page, name) {
  await page.locator('#auth-switch').click();
  await page.locator('#username').fill(name);
  await page.locator('#password').fill('Workbench2026');
  await page.locator('#password-confirm').fill('different');
  await page.locator('#auth-submit').click();
  await page.locator('#auth-error').getByText('两次输入的密码不一致。').waitFor();
  await page.locator('#password-confirm').fill('Workbench2026');
  await page.locator('#auth-submit').click();
  await page.locator('#workspace').waitFor();
}

async function profile(page) {
  await page.locator('#edit-profile').click();
  const form = page.locator('#profile-form');
  await form.locator('[name=province]').fill('广东');
  await form.locator('[name=subject_type]').selectOption('物理类');
  await form.locator('[name=target_year]').fill('2027');
  await form.locator('[name=province_rank]').fill('20000');
  await form.getByLabel('化学', {exact: true}).check();
  await form.locator('[name=preferred_cities]').fill('广州');
  await form.locator('[name=excluded_cities]').fill('广州');
  await form.getByRole('button', {name: '保存条件'}).click();
  await page.locator('#profile-error').getByText('偏好城市与排除城市不能重叠').waitFor();
  await form.locator('[name=excluded_cities]').fill('深圳');
  await form.getByRole('button', {name: '保存条件'}).click();
  await page.locator('#profile-dialog').waitFor({state: 'hidden'});
  await page.locator('#profile-summary').getByText(/省位次 20,000/).waitFor();
}

async function searchHistory(page) {
  await page.locator('#history-open').click();
  await page.locator('#history-form [name=major]').fill('计算机');
  await page.getByRole('button', {name: '查询', exact: true}).click();
  await page.locator('#history-status').getByText(/共 12 条候选/).waitFor();
  assert.match(await page.locator('#history-status').innerText(), /按 2025 年记录/);
  await page.getByRole('button', {name: '加入对照'}).first().click();
  await page.locator('#history-next').click();
  await page.locator('#history-status').getByText(/11–12/).waitFor();
  await page.getByRole('button', {name: '加入对照'}).first().click();
  await page.getByRole('table', {name: '历史录取候选对照'}).waitFor();
  const comparison = await page.locator('#comparison-table').innerText();
  assert.match(comparison, /大学01/);
  assert.match(comparison, /大学11/);
  await page.getByRole('button', {name: '放入对话'}).first().click();
  const draft = await page.locator('#message').inputValue();
  await page.reload();
  await page.locator('#workspace').waitFor();
  assert.equal(await page.locator('#message').inputValue(), draft);
}

async function send(page, text) {
  const input = page.locator('#message');
  await input.fill(text);
  await page.locator('#send').click();
  await page.locator('.message.user').getByText(text, {exact: true}).last().waitFor();
}

async function manageResearch(page) {
  const loseResponse = async route => {
    await route.fetch();
    await route.abort();
  };
  await page.route('**/sessions/*/messages', loseResponse, {times: 1});
  await page.locator('#message').fill('研究计算机方案');
  await page.locator('#send').click();
  await page.locator('#notice').getByText(/无法连接服务/).waitFor();
  assert.equal(await page.locator('#message').inputValue(), '研究计算机方案');
  await page.locator('#send').click();
  await page.getByRole('button', {name: '确认并继续'}).waitFor();
  assert.equal(await page.locator('.message.user').getByText('研究计算机方案', {exact: true}).count(), 1);
  await page.getByRole('button', {name: '略过'}).click();
  await page.getByRole('button', {name: '确认并继续'}).waitFor({state: 'hidden'});
  await send(page, '重新研究计算机');
  await page.getByRole('button', {name: '确认并继续'}).click();
  await page.getByText(/模型服务额度不足/).waitFor();
  await page.reload();
  await page.getByRole('button', {name: '重试研究', exact: true}).click();
  await page.getByText('已保存浏览器验收方案。', {exact: true}).waitFor();
  await page.getByRole('button', {name: /志愿方案 · 第 1 版/}).click();
  await page.getByRole('table', {name: '有序候选'}).waitFor();
  await page.getByRole('heading', {name: /资料来源/}).waitFor();
  const download = page.waitForEvent('download');
  await page.locator('#export-artifact').click();
  const file = await fs.readFile(await (await download).path(), 'utf8');
  assert.match(file, /浏览器验收大学01/);
  assert.match(file, /历史录取/);
  await page.locator('#artifact-dialog [data-close]').click();
  await send(page, '记录数学兴趣');
  await page.getByText('已记录数学兴趣。', {exact: true}).waitFor();
  await page.locator('#open-memory').click();
  await page.getByLabel('兴趣', {exact: true}).fill('应用数学');
  await page.getByRole('button', {name: '保存更正'}).click();
  await until(async () => await page.getByLabel('兴趣', {exact: true}).inputValue() === '应用数学', '偏好更正');
  await page.getByRole('button', {name: '删除', exact: true}).click();
  await page.locator('#memories').getByText('暂无已记录的偏好。').waitFor();
  await page.locator('#memory-dialog [data-close]').click();
}

async function verifyConversations(page) {
  const first = page.url();
  await page.locator('#session-settings').click();
  await page.locator('#session-title').fill('院校研究');
  await page.getByRole('button', {name: '保存标题'}).click();
  await page.getByRole('heading', {name: '院校研究', exact: true}).waitFor();
  await page.locator('#new-session').click();
  await page.route('**/runs/*/events?after=*', route => route.abort(), {times: 1});
  await send(page, '测试停止研究');
  await page.getByText('研究中', {exact: true}).waitFor();
  const second = page.url();
  await page.locator('#sessions').getByRole('button', {name: '院校研究', exact: true}).click();
  await page.locator('#other-run').waitFor();
  await page.locator('#message').fill('另一个会话的草稿');
  assert.equal(await page.locator('#send').isDisabled(), true);
  await page.locator('#go-to-run').click();
  assert.equal(page.url(), second);
  await page.reload();
  await page.locator('#cancel').click();
  await page.getByText('已停止', {exact: true}).first().waitFor({timeout: 18000});
  let release;
  const hold = new Promise(resolve => { release = resolve; });
  const firstId = new URL(first).searchParams.get('session');
  let routeEntered;
  const entered = new Promise(resolve => { routeEntered = resolve; });
  await page.route('**/api/v2/agent/sessions/' + firstId, async route => {
    routeEntered();
    await hold;
    await route.continue();
  }, {times: 1});
  await page.locator('#sessions').getByRole('button', {name: '院校研究', exact: true}).click();
  await entered;
  await page.locator('#sessions').getByRole('button', {name: '测试停止研究', exact: true}).click();
  const late = page.waitForResponse(response => response.url().endsWith('/sessions/' + firstId));
  release();
  await late;
  await page.getByRole('heading', {name: '测试停止研究', exact: true}).waitFor();
  assert.equal(await page.getByRole('button', {name: /志愿方案 · 第 1 版/}).count(), 0);
  await page.goto(first);
  await page.locator('#workspace').waitFor();
  assert.equal(await page.locator('#message').inputValue(), '另一个会话的草稿');
  await page.getByRole('button', {name: /志愿方案 · 第 1 版/}).waitFor();
  await page.locator('#session-settings').click();
  await page.locator('#archive-session').click();
  await page.getByText('此对话已归档。').waitFor();
  await page.reload();
  await page.getByText('此对话已归档。').waitFor();
  await page.locator('#restore-session').click();
  await page.locator('#message').waitFor();
  return first;
}

async function liveRun(page, base, submit) {
  const [response] = await Promise.all([
    page.waitForResponse(res => /\/sessions\/[^/]+\/messages$/.test(res.url()) && res.request().method() === 'POST'),
    submit(),
  ]);
  assert.ok(response.ok(), '真实研究请求未被接受，HTTP ' + response.status());
  const {id} = await response.json();
  const token = await page.evaluate(() => localStorage.getItem('gaokao_auth_token'));
  const deadline = Date.now() + 310000;
  while (Date.now() < deadline) {
    const status = await page.request.get(base + '/api/v2/agent/runs/' + id, {
      headers: {Authorization: 'Bearer ' + token},
    });
    assert.ok(status.ok(), '无法读取真实研究状态，HTTP ' + status.status());
    const run = await status.json();
    if (run.result) return run.result;
    await sleep(750);
  }
  throw new Error('真实研究在 310 秒内未结束');
}

async function verifyLive(page, base) {
  await register(page, 'browser_live');
  await profile(page);
  await page.locator('#history-open').click();
  await page.locator('#history-form [name=school]').fill('北京邮电大学');
  await page.locator('#history-form [name=major]').fill('计算机');
  await page.getByRole('button', {name: '查询', exact: true}).click();
  await page.locator('#history-results').getByRole('heading', {name: /北京邮电大学/}).first().waitFor();
  await page.locator('#history-dialog [data-close]').click();
  console.log('真实历史库查询通过');

  const first = await liveRun(page, base, () => send(page,
    '请按我已保存的考生条件研究北京邮电大学的计算机相关专业，查数据库并给出一份带历史来源的志愿草案。请先请求我确认调查。'));
  await page.locator('#messages').getByText(first.text, {exact: true}).waitFor();
  assert.equal(first.status, 'waiting', '真实模型尚未到达调查确认环节。' + (first.provider_error || first.status));
  assert.ok(first.confirmation_id, '真实模型未提供调查确认编号');
  const card = page.locator('#confirmations .confirmation').filter({hasText: first.text});
  const finished = await liveRun(page, base, () => card.getByRole('button', {name: '确认并继续'}).click());
  assert.equal(finished.status, 'completed', '真实模型尚未完成方案。' + (finished.provider_error || finished.status));
  await page.getByRole('button', {name: /志愿方案 · 第 \d+ 版/}).first().click();
  await page.getByRole('table', {name: '有序候选'}).waitFor();
  await page.getByRole('heading', {name: /资料来源/}).waitFor();
  assert.match(await page.locator('#artifact-content').innerText(), /北京邮电大学/);
  const download = page.waitForEvent('download');
  await page.locator('#export-artifact').click();
  const file = await fs.readFile(await (await download).path(), 'utf8');
  assert.match(file, /北京邮电大学/);
  console.log('真实模型浏览器验收通过，已核对方案、历史来源和下载文件。');
}

async function main() {
  const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'gaokao-workbench-'));
  const port = await freePort();
  const base = 'http://127.0.0.1:' + port;
  const python = process.env.GAOKAO_TEST_PYTHON || path.join(root, '.venv', 'bin', 'python');
  const backend = path.join(root, 'backend');
  const serverArgs = live
    ? ['-c', 'import sys, uvicorn; from database.import_data import run_import; run_import(); uvicorn.run("main:app", host="127.0.0.1", port=int(sys.argv[1]))', String(port)]
    : ['-m', 'uvicorn', 'browser_fixture:app', '--host', '127.0.0.1', '--port', String(port)];
  const server = spawn(python, serverArgs, {
    cwd: backend,
    env: {...process.env, PYTHONPATH: path.join(backend, 'tests'), PYTHONDONTWRITEBYTECODE: '1',
      DATABASE_URL: 'sqlite:///' + path.join(folder, 'browser.db'), PROFILES_DIR: path.join(folder, 'profiles'),
      JWT_SECRET_KEY: 'isolated-browser-test', AGENT_EMBEDDED_WORKER: 'true'},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  server.on('error', error => { output += error.message; });
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  let browser;
  try {
    await until(async () => (await fetch(base + '/api/health').catch(() => null))?.ok, '测试服务启动');
    browser = await chromium.launch({channel: process.env.GAOKAO_TEST_BROWSER || undefined, headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 900}, acceptDownloads: true});
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    if (live) {
      await verifyLive(page, base);
      assert.deepEqual(errors, []);
      return;
    }
    const invalid = await page.request.post(base + '/api/v1/auth/register', {
      data: {username: 'browser_invalid', password: 'sensitive-password'},
    });
    assert.equal(invalid.status(), 422);
    assert.doesNotMatch(await invalid.text(), /sensitive-password/);
    await register(page, 'browser_one');
    await profile(page);
    await searchHistory(page);
    await manageResearch(page);
    const privateUrl = await verifyConversations(page);

    const other = await browser.newContext({viewport: {width: 375, height: 812}, isMobile: true, hasTouch: true});
    const mobile = await other.newPage();
    await mobile.goto(privateUrl);
    await register(mobile, 'browser_two');
    await mobile.locator('#notice').getByText('当前账号无法打开该对话。').waitFor();
    assert.equal(await mobile.locator('.message').count(), 0);
    await mobile.locator('#toggle-details').click();
    await mobile.locator('#inspector').getByRole('heading', {name: '研究成果'}).waitFor();
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth), 375);
    await mobile.locator('#mobile-history').click();
    await mobile.getByRole('heading', {name: '历年录取查询'}).waitFor();

    const sameAccount = await context.newPage();
    await sameAccount.goto(privateUrl);
    await sameAccount.locator('#workspace').waitFor();
    await page.locator('#logout').click();
    await sameAccount.locator('#auth-form').waitFor();
    assert.equal(await sameAccount.locator('#messages').textContent(), '');
    await page.locator('#username').fill('browser_one');
    await page.locator('#password').fill('Workbench2026');
    await page.locator('#auth-submit').click();
    await page.getByRole('heading', {name: '院校研究', exact: true}).waitFor();
    const imported = await page.evaluate(async () => {
      const token = localStorage.getItem('gaokao_auth_token');
      const response = await fetch('/api/v1/profile', {
        method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token},
        body: JSON.stringify({province: '浙江', subject_type: '物理类', province_rank: 12000, gaokao_score: 603}),
      });
      return (await response.json()).data.uuid;
    });
    await page.goto(base + '/chat?profile=' + imported);
    await page.locator('#session-context').getByText(/浙江.*12,000/).waitFor();
    assert.equal(new URL(page.url()).searchParams.has('profile'), false);
    await page.goto(base + '/form');
    assert.equal(await page.locator('a[href="/chat"]').count(), 1);
    assert.deepEqual(errors, []);
    console.log('通过浏览器验收：注册登录、画像、录取对照、草稿恢复、确认、方案与下载、记忆、停止、归档、账号隔离、移动端。');
  } catch (error) {
    console.error(output.split('\n').slice(-45).join('\n'));
    throw error;
  } finally {
    await browser?.close();
    if (server.exitCode === null && server.pid) {
      const exited = once(server, 'exit');
      server.kill();
      await exited;
    }
    await fs.rm(folder, {recursive: true, force: true});
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
