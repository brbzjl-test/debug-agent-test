// Exercise the production polling functions with controlled asynchronous responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '../src/field_support_agent/ui/web/app.js'), 'utf8');
const polling = script.slice(script.indexOf('  let analysisPollVersion = 0;'), script.indexOf('  async function pollHumanResult'));

function setup(timeline) {
  const frames = [];
  const state = { current: { id: 'ISS-1' }, phase: 'analyzing', messages: [
    { role: 'user', content: '没有反应' }, { role: 'assistant', typing: true },
  ] };
  const capture = () => frames.push(JSON.parse(JSON.stringify({ phase: state.phase, messages: state.messages })));
  const prompts = [];
  const input = { set placeholder(value) { prompts.push(value); } };
  const hint = { textContent: '', classList: { toggle: () => {} } };
  const context = vm.createContext({
    state, api: { timeline }, console: { warn: () => {} }, renderMessages: capture, renderChat: capture,
    renderWorkflow: capture, window: { setTimeout: (callback) => queueMicrotask(callback) },
    messageInput: input, composer: { querySelector: () => hint },
  });
  vm.runInContext(polling, context);
  return { context, frames, state, prompts, hint };
}

test('renders partial answers while locked, then replaces them with a single completed message', async () => {
  const responses = [
    { issue: { status: 'open' }, messages: [], analysis: { status: 'running', content: '请' } },
    { issue: { status: 'open' }, messages: [], analysis: { status: 'running', content: '请检查' } },
    { issue: { status: 'open' }, messages: [{ role: 'assistant', content: '请检查连接线。' }] },
  ];
  const { context, frames, state } = setup(async () => {
    assert.ok(responses.length, 'should stop polling after final message');
    return responses.shift();
  });
  await vm.runInContext("pollTimeline('ISS-1', 0)", context);
  assert.equal(frames[0].phase, 'analyzing');
  assert.equal(frames[0].messages.at(-1).content, '请');
  assert.equal(frames[1].messages.at(-1).content, '请检查');
  assert.equal(state.phase, 'result');
  assert.equal(state.messages.filter((m) => m.role === 'assistant').length, 1);
  assert.equal(state.messages.at(-1).content, '请检查连接线。');
  assert.equal(state.messages.at(-1).streaming, undefined);
});

test('ignores an old issue response after switching to a different issue', async () => {
  let release, requested;
  const pending = new Promise((resolve) => { release = resolve; });
  const started = new Promise((resolve) => { requested = resolve; });
  const { context, state, frames } = setup(() => { requested(); return pending; });
  const done = vm.runInContext("pollTimeline('ISS-1', 0)", context);
  await started;
  state.current = { id: 'ISS-2' };
  release({ issue: { status: 'open' }, messages: [{ role: 'assistant', content: '旧问题回答' }] });
  await done;
  assert.equal(frames.length, 0);
  assert.equal(state.current.id, 'ISS-2');
});

test('an interrupted stream is replaced by the persisted failure message', async () => {
  const responses = [
    { issue: { status: 'open' }, messages: [], analysis: { status: 'running', content: '未完成回答' } },
    { issue: { status: 'open' }, messages: [{ role: 'assistant', content: '本次分析没有完成' }] },
  ];
  const { context, state } = setup(async () => responses.shift());
  await vm.runInContext("pollTimeline('ISS-1', 0)", context);
  assert.equal(state.phase, 'result');
  assert.equal(state.messages.at(-1).content, '本次分析没有完成');
  assert.ok(!state.messages.some((m) => m.streaming));
});

test('reports observed work and a long silence without claiming a network failure', () => {
  const { context } = setup(() => {});
  const status = (progress) => {
    context.progress = progress;
    return vm.runInContext('analysisStatus(progress)', context);
  };
  assert.equal(status({ stage: 'reading_code', elapsed_seconds: 18, idle_seconds: 0 }).prompt, '正在检查业务代码');
  const silent = status({ stage: 'reading_code', elapsed_seconds: 40, idle_seconds: 22 });
  assert.equal(silent.waiting, true);
  assert.match(silent.prompt, /等待 Codex 响应/);
  assert.match(silent.hint, /22 秒未收到新进展/);
  assert.match(silent.hint, /可能/);
  const resumed = status({ stage: 'reading_logs', elapsed_seconds: 41, idle_seconds: 0 });
  assert.equal(resumed.prompt, '正在读取日志');
  assert.equal(resumed.waiting, false);
  assert.match(status({ stage: 'limited', elapsed_seconds: 40, idle_seconds: 40 }).hint, /不提供中间进度/);
  assert.doesNotMatch(status({ stage: 'capturing', elapsed_seconds: 40, idle_seconds: 40 }).prompt, /Codex/);
});

test('local connection failure is displayed and clears when progress resumes', async () => {
  let calls = 0;
  const { context, frames, prompts } = setup(async () => {
    calls++;
    if (calls === 1) throw new Error('local API disconnected');
    if (calls === 2) return { issue: { status: 'open' }, messages: [], analysis: {
      status: 'running', stage: 'reading_logs', elapsed_seconds: 12, idle_seconds: 0,
    } };
    return { issue: { status: 'open' }, messages: [{ role: 'assistant', content: '检查完成' }] };
  });
  await vm.runInContext("pollTimeline('ISS-1', 0)", context);
  assert.match(frames[0].messages.at(-1).progress.prompt, /无法连接本机服务/);
  assert.equal(frames[1].messages.at(-1).progress.prompt, '正在读取日志');
  assert.deepEqual(prompts, [], 'progress belongs in the assistant bubble, not the composer');
});

test('one assistant bubble changes from work status to streaming text to the completed reply', async () => {
  const responses = [
    { analysis: { status: 'running', stage: 'reading_logs', content: '', elapsed_seconds: 5, idle_seconds: 0 } },
    { analysis: { status: 'running', stage: 'responding', content: '请检查', elapsed_seconds: 6, idle_seconds: 0 } },
    { analysis: { status: 'running', stage: 'responding', content: '请检查', elapsed_seconds: 28, idle_seconds: 22 } },
    { messages: [{ role: 'assistant', content: '请检查连接线。' }] },
  ];
  const { context, frames, state } = setup(async () => ({ issue: { status: 'open' }, ...responses.shift() }));
  await vm.runInContext("pollTimeline('ISS-1', 0)", context);
  for (const frame of frames) assert.equal(frame.messages.filter((m) => m.role === 'assistant').length, 1);
  assert.equal(frames[0].messages.at(-1).progress.prompt, '正在读取日志');
  assert.equal(frames[1].messages.at(-1).content, '请检查');
  assert.equal(frames[2].messages.at(-1).progress.waiting, true);
  assert.equal(state.messages.at(-1).progress, undefined);
  assert.equal(state.messages.at(-1).content, '请检查连接线。');
});
