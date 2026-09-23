(() => {
  'use strict';

  const STATUS_LABELS = {
    open: '处理中',
    pending_verification: '待验证',
    closed: '已解决',
  };

  const DEMO_ISSUES = [
    { id: 'ISS-20260915-7F3A1C20', rootId: 'ISS-20260915-7F3A1C20', summary: '程序启动后没有读取到设备', status: 'open', date: '今天 09:42', nextSub: 1 },
    { id: 'ISS-20260912-A8D921B4', rootId: 'ISS-20260912-A8D921B4', summary: '脚踏有指示灯，但操作没有反应', status: 'pending_verification', date: '9月12日', nextSub: 2 },
    { id: 'ISS-20260908-14C97E52', rootId: 'ISS-20260908-14C97E52', summary: '运行一段时间后界面停止响应', status: 'closed', date: '9月8日', nextSub: 1 },
  ];

  const DEMO_SETTINGS = {
    version: 1,
    business: { repositories: [{ name: 'debug-agent-test', git_url: 'git@github.com:brbzjl-test/debug-agent-test.git', local_path: '/Users/brb/项目/debug-agent-test' }], ros_topology_file: '', log_paths: [] },
    codex: { binary: '/Applications/ChatGPT.app/Contents/Resources/codex', home: '/Users/brb/.codex', model: '', reasoning_effort: '', timeout_seconds: 300 },
    feishu: { device_name: '', site_name: '', connection_mode: 'credentials', app_id: '', app_secret_configured: false, lark_cli_binary: '', lark_profile: '', support_chat_id: '', base_app_token: '', base_table_id: '' },
    management: { password_configured: false },
  };

  const DEMO_BUSINESS_STATUS = {
    checked_at: new Date().toISOString(),
    available: true,
    summary: { total: 1, running: 1 },
    programs: [{
      name: 'debug-agent-test',
      local_path: '/Users/brb/项目/debug-agent-test',
      state: 'running',
      process_count: 1,
      pids: [28142],
      processes: [{ pid: 28142, elapsed_seconds: 8172 }],
    }],
  };

  class CoreApi {
    constructor() {
      this.base = '/api/v1';
      this.demo = new URLSearchParams(location.search).has('demo') || location.protocol === 'file:';
      this.reporterId = localStorage.getItem('field-support-reporter-id') || 'local-site-user';
    }

    async request(path, options = {}) {
      if (this.demo) return null;
      const { headers = {}, ...requestOptions } = options;
      const response = await fetch(`${this.base}${path}`, {
        ...requestOptions,
        headers: { 'Content-Type': 'application/json', ...headers },
      });
      if (!response.ok) {
        let message = `Core API ${response.status}`;
        try {
          const payload = await response.json();
          message = payload.error?.message || message;
        } catch (_) { /* response is not JSON */ }
        throw new Error(message);
      }
      return response.status === 204 ? null : response.json();
    }

    async createIssue() {
      const result = await this.request('/issues', {
        method: 'POST', body: JSON.stringify({ reporter_id: this.reporterId }),
      });
      return result ? this.normalizeIssue(result.issue) : { id: makeIssueId(), rootId: null, status: 'open', created_at: new Date().toISOString() };
    }

    async createSubIssue(rootId, observation = '') {
      const result = await this.request(`/issues/${encodeURIComponent(rootId)}/subissues`, {
        method: 'POST', body: JSON.stringify({ reporter_id: this.reporterId, description: observation || undefined }),
      });
      return result ? this.normalizeIssue(result.issue) : null;
    }

    async appendMessage(issueId, content) {
      return this.request(`/issues/${encodeURIComponent(issueId)}/messages`, {
        method: 'POST', body: JSON.stringify({ actor_id: this.reporterId, role: 'reporter', content }),
      });
    }

    async requestHandoff(issueId) {
      const result = await this.request(`/issues/${encodeURIComponent(issueId)}/handoff`, {
        method: 'POST', body: JSON.stringify({ actor_id: this.reporterId }),
      });
      return result ? this.normalizeIssue(result.issue) : null;
    }

    async confirmSolution(issueId, solutionVersion) {
      return this.request(`/issues/${encodeURIComponent(issueId)}/confirm`, {
        method: 'POST', body: JSON.stringify({ reporter_id: this.reporterId, expected_solution_version: solutionVersion }),
      });
    }

    async reportVerificationFailure(issueId, observation) {
      const result = await this.request(`/issues/${encodeURIComponent(issueId)}/verification-failure`, {
        method: 'POST', body: JSON.stringify({ reporter_id: this.reporterId, observation }),
      });
      return result ? this.normalizeIssue(result.issue) : { status: 'open' };
    }

    async confirmAiResolution(issueId) {
      const result = await this.request(`/issues/${encodeURIComponent(issueId)}/confirm-ai-resolution`, {
        method: 'POST', body: JSON.stringify({ reporter_id: this.reporterId }),
      });
      return result ? this.normalizeIssue(result.issue) : { status: 'closed' };
    }

    async listIssues() {
      const result = await this.request('/issues');
      return result ? result.issues.map((issue) => this.normalizeIssue(issue)) : DEMO_ISSUES;
    }

    async timeline(issueId) {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 6000);
      try {
        return await this.request(`/issues/${encodeURIComponent(issueId)}/timeline`, { signal: controller.signal });
      } finally {
        window.clearTimeout(timeout);
      }
    }

    async businessStatus() {
      const result = await this.request('/business-status');
      return result ? result.business_status : structuredClone(DEMO_BUSINESS_STATUS);
    }

    async getSettings() {
      const result = await this.request('/settings');
      if (result) return result.settings;
      const settings = structuredClone(DEMO_SETTINGS);
      settings.management.password_configured = Boolean(localStorage.getItem('field-support-demo-management-password'));
      return settings;
    }

    async getStartup() {
      if (this.demo) return { available: false, enabled: false };
      return this.request('/startup');
    }

    async setStartup(enabled) {
      return this.request('/startup', { method: 'PUT', body: JSON.stringify({ enabled }) });
    }

    async saveSettings(settings) {
      if (this.demo && settings.management?.password) {
        localStorage.setItem('field-support-demo-management-password', settings.management.password);
        settings.management = { password_configured: true };
      }
      const result = await this.request('/settings', { method: 'PUT', body: JSON.stringify(settings) });
      return result ? result.settings : settings;
    }

    async verifyFeishu(settings) {
      if (this.demo) return { verified: true };
      return this.request('/settings/verify-feishu', {
        method: 'POST', body: JSON.stringify(settings),
      });
    }

    async managementStatus() {
      if (this.demo) return { configured: Boolean(localStorage.getItem('field-support-demo-management-password')) };
      return this.request('/management/status');
    }

    async unlockManagement(password) {
      if (this.demo) {
        if (password !== localStorage.getItem('field-support-demo-management-password')) throw new Error('管理密码错误');
        return { management_token: 'demo-management-token', password_created: false, expires_in: 1800 };
      }
      return this.request('/management/session', {
        method: 'POST', body: JSON.stringify({ password }),
      });
    }

    async lockManagement(token) {
      if (this.demo) return;
      return this.request('/management/session/close', {
        method: 'POST',
        headers: { 'X-Management-Token': token },
        body: JSON.stringify({}),
      });
    }

    async deleteIssues(issueIds, token) {
      if (this.demo) return { deleted_issue_ids: issueIds };
      return this.request('/management/issues/delete', {
        method: 'POST',
        headers: { 'X-Management-Token': token },
        body: JSON.stringify({ issue_ids: issueIds }),
      });
    }

    normalizeIssue(issue) {
      return {
        id: issue.issue_id,
        rootId: issue.root_issue_id,
        parentId: issue.parent_issue_id,
        status: issue.status,
        date: new Date(issue.created_at).toLocaleString('zh-CN'),
        summary: issue.summary || '等待描述问题',
        nextSub: (issue.sub_sequence || 0) + 1,
        latestSolutionVersion: issue.latest_solution_version || 0,
        handoffState: issue.handoff_state || 'none',
      };
    }
  }

  const api = new CoreApi();
  const state = {
    screen: 'init',
    filter: 'all',
    issues: loadIssues(),
    current: null,
    selectedForReopen: null,
    detailIssue: null,
    detailTimeline: null,
    detailOrigin: 'history',
    chatOrigin: 'init',
    detailError: '',
    phase: 'ready',
    messages: [],
    analysisProgress: null,
    settingsLoaded: false,
    businessStatusLoading: false,
    managementMode: false,
    managementToken: '',
    managementConfigured: false,
    selectedIssueIds: new Set(),
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const screens = $$('.screen');
  const backButton = $('#back-button');
  const composer = $('#composer');
  const messageInput = $('#message-input');
  const workflowPanel = $('#workflow-panel');

  if (window.qt && window.QWebChannel) {
    new QWebChannel(qt.webChannelTransport, (channel) => {
      window.desktopBridge = channel.objects.desktopBridge;
    });
  }

  function loadIssues() {
    if (!new URLSearchParams(location.search).has('demo') && location.protocol !== 'file:') return [];
    try {
      const saved = JSON.parse(localStorage.getItem('field-support-demo-issues') || 'null');
      return Array.isArray(saved) && saved.length ? saved : DEMO_ISSUES;
    } catch (_) {
      return DEMO_ISSUES;
    }
  }

  function saveIssues() {
    localStorage.setItem('field-support-demo-issues', JSON.stringify(state.issues));
  }

  function makeIssueId() {
    const day = new Date().toISOString().slice(0, 10).replaceAll('-', '');
    const random = crypto.getRandomValues(new Uint32Array(1))[0].toString(16).toUpperCase().padStart(8, '0');
    return `ISS-${day}-${random.slice(0, 8)}`;
  }

  function nextSubId(issue) {
    const root = issue.rootId || issue.id.split('-S')[0];
    return `${root}-S${String(issue.nextSub || 1).padStart(3, '0')}`;
  }

  function navigate(screen) {
    state.screen = screen;
    screens.forEach((element) => element.classList.toggle('is-active', element.dataset.screen === screen));
    backButton.classList.toggle('is-hidden', screen === 'init');
    if (screen === 'init') {
      renderIssueLists();
      loadBusinessStatus();
    }
    if (screen === 'history') renderHistory();
    if (screen === 'detail') renderIssueDetail();
    if (screen === 'chat') renderChat();
    if (screen === 'settings') loadSettings();
  }

  async function loadSettings() {
    if (state.settingsLoaded) return;
    setSettingsResult('正在读取设置');
    try {
      const [settings, startup] = await Promise.all([api.getSettings(), api.getStartup()]);
      renderSettings(settings);
      renderStartup(startup);
      state.settingsLoaded = true;
      setSettingsResult('');
    } catch (error) {
      setSettingsResult(error.message, true);
    }
  }

  async function loadBusinessStatus() {
    if (state.businessStatusLoading) return;
    state.businessStatusLoading = true;
    renderBusinessStatus(null);
    try {
      renderBusinessStatus(await api.businessStatus());
    } catch (_) {
      renderBusinessStatus({ available: false, summary: { total: 0, running: 0 }, programs: [] });
    } finally {
      state.businessStatusLoading = false;
    }
  }

  function renderBusinessStatus(report) {
    const title = $('#business-status-title');
    const copy = $('#business-status-copy');
    const badge = $('#business-status-badge');
    const list = $('#business-status-list');
    if (!report) {
      title.textContent = '正在检查业务程序';
      copy.textContent = '按设置中的本地代码路径检测';
      badge.textContent = '检查中';
      badge.className = 'status-badge status-unknown';
      list.replaceChildren();
      return;
    }

    const total = report.summary?.total || report.programs?.length || 0;
    const running = report.summary?.running || 0;
    if (!report.available) {
      title.textContent = '暂时无法检测业务程序';
      badge.textContent = '无法检测';
      badge.className = 'status-badge status-unknown';
    } else if (total > 0 && running === total) {
      title.textContent = `${running} 个业务程序正在运行`;
      badge.textContent = '运行中';
      badge.className = 'status-badge status-ok';
    } else if (running > 0) {
      title.textContent = `${running}/${total} 个业务程序正在运行`;
      badge.textContent = '部分运行';
      badge.className = 'status-badge status-partial';
    } else {
      title.textContent = total ? '未检测到业务程序进程' : '尚未配置业务程序';
      badge.textContent = total ? '未检测到' : '未配置';
      badge.className = `status-badge ${total ? 'status-stopped' : 'status-unknown'}`;
    }
    copy.textContent = report.checked_at
      ? `最近检查 ${formatDetailDate(report.checked_at)}`
      : '按设置中的本地代码路径检测';
    list.replaceChildren(...(report.programs || []).map((program) => {
      const item = document.createElement('div');
      item.className = 'business-status-item';
      const name = document.createElement('strong');
      name.textContent = program.name;
      const status = document.createElement('span');
      status.className = `program-state ${program.state}`;
      status.textContent = program.state === 'running' ? '运行中' : (program.state === 'not_running' ? '未检测到' : '无法检测');
      const metadata = document.createElement('small');
      metadata.className = 'process-metadata';
      const path = document.createElement('small');
      path.className = 'program-path';
      const processIds = Array.isArray(program.pids) && program.pids.length ? program.pids.join(', ') : '—';
      const runtimes = (program.processes || [])
        .map((process) => process.elapsed_seconds)
        .filter((seconds) => Number.isFinite(seconds));
      let duration = '—';
      if (runtimes.length === 1) duration = formatDuration(runtimes[0]);
      if (runtimes.length > 1) {
        const shortest = Math.min(...runtimes);
        const longest = Math.max(...runtimes);
        duration = shortest === longest
          ? formatDuration(shortest)
          : `${formatDuration(shortest)}–${formatDuration(longest)}`;
      }
      const instanceCount = program.state === 'unknown' ? '—' : String(program.process_count || 0);
      metadata.textContent = `${instanceCount} 个实例 · PID ${processIds} · 运行时长 ${duration}`;
      path.textContent = program.local_path;
      item.append(name, status, metadata, path);
      return item;
    }));
  }

  function formatDuration(value) {
    const seconds = Math.max(0, Math.floor(Number(value)));
    if (!Number.isFinite(seconds)) return '—';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return `${days}天${hours ? `${hours}小时` : ''}`;
    if (hours) return `${hours}小时${minutes ? `${minutes}分钟` : ''}`;
    if (minutes) return `${minutes}分钟`;
    return `${seconds}秒`;
  }

  function renderSettings(settings) {
    const repositories = $('#repository-settings');
    repositories.replaceChildren();
    settings.business.repositories.forEach((repository) => addRepositoryRow(repository));
    $('#ros-topology-file').value = settings.business.ros_topology_file || '';
    $('#business-log-paths').value = (settings.business.log_paths || []).join('\n');
    $('#codex-binary').value = settings.codex.binary || '';
    $('#codex-home').value = settings.codex.home || '';
    $('#codex-model').value = settings.codex.model || '';
    $('#codex-effort').value = settings.codex.reasoning_effort || '';
    $('#codex-timeout').value = settings.codex.timeout_seconds || 300;
    $('#codex-state').textContent = settings.codex.binary ? '已配置' : '未配置';
    $('#site-name').value = settings.feishu.site_name || '';
    $('#device-name').value = settings.feishu.device_name || '';
    $('#feishu-connection-mode').value = settings.feishu.connection_mode || 'credentials';
    $('#feishu-app-id').value = settings.feishu.app_id || '';
    $('#feishu-app-secret').value = '';
    $('#feishu-app-secret').placeholder = settings.feishu.app_secret_configured ? '已配置，留空不修改' : '请输入 App Secret';
    $('#lark-cli-binary').value = settings.feishu.lark_cli_binary || '';
    $('#lark-profile').value = settings.feishu.lark_profile || '';
    $('#feishu-chat-id').value = settings.feishu.support_chat_id || '';
    $('#base-app-token').value = settings.feishu.base_app_token || '';
    $('#base-table-id').value = settings.feishu.base_table_id || '';
    $('#management-password-setting').value = '';
    $('#management-password-setting').placeholder = settings.management?.password_configured ? '已配置，留空不修改' : '输入至少 4 个字符';
    $('#management-password-state').textContent = settings.management?.password_configured ? '已配置' : '未配置';
    updateFeishuMode();
    const profileReady = settings.feishu.connection_mode === 'lark_cli_profile' && settings.feishu.lark_cli_binary && settings.feishu.lark_profile;
    $('#feishu-state').textContent = profileReady || settings.feishu.app_secret_configured ? '已配置' : '未配置';
  }

  function renderStartup(startup) {
    const toggle = $('#startup-enabled');
    toggle.disabled = !startup.available;
    toggle.checked = Boolean(startup.enabled);
    $('#startup-state').textContent = startup.available ? (startup.enabled ? '已开启' : '已关闭') : '仅 Ubuntu 安装版';
  }

  async function changeStartup(event) {
    const toggle = event.currentTarget;
    const previous = !toggle.checked;
    toggle.disabled = true;
    setSettingsResult('正在保存自启动设置');
    try {
      renderStartup(await api.setStartup(toggle.checked));
      setSettingsResult('自启动设置已保存，下次开机生效', false, true);
    } catch (error) {
      toggle.checked = previous;
      toggle.disabled = false;
      setSettingsResult(error.message, true);
    }
  }

  function updateFeishuMode() {
    const profileMode = $('#feishu-connection-mode').value === 'lark_cli_profile';
    $('#feishu-credentials-fields').classList.toggle('is-hidden', profileMode);
    $('#feishu-profile-fields').classList.toggle('is-hidden', !profileMode);
  }

  function addRepositoryRow(repository = {}) {
    const entry = document.createElement('div');
    entry.className = 'repository-entry';
    entry.innerHTML = `
      <label class="field-row"><span>名称</span><input class="repository-name" type="text" autocomplete="off" required></label>
      <label class="field-row"><span>Git 地址</span><input class="repository-url" type="text" autocomplete="off" required></label>
      <label class="field-row repository-path"><span>本地路径</span><input class="repository-local-path" type="text" autocomplete="off" required></label>
      <button class="remove-repository" type="button" aria-label="移除仓库" title="移除仓库">×</button>`;
    entry.querySelector('.repository-name').value = repository.name || '';
    entry.querySelector('.repository-url').value = repository.git_url || '';
    entry.querySelector('.repository-local-path').value = repository.local_path || '';
    entry.querySelector('.remove-repository').addEventListener('click', () => {
      if ($$('.repository-entry').length > 1) entry.remove();
    });
    $('#repository-settings').append(entry);
  }

  function collectSettings() {
    const repositories = $$('.repository-entry').map((entry) => ({
      name: entry.querySelector('.repository-name').value.trim(),
      git_url: entry.querySelector('.repository-url').value.trim(),
      local_path: entry.querySelector('.repository-local-path').value.trim(),
    }));
    const feishu = {
      device_name: $('#device-name').value.trim(),
      site_name: $('#site-name').value.trim(),
      connection_mode: $('#feishu-connection-mode').value,
      app_id: $('#feishu-app-id').value.trim(),
      lark_cli_binary: $('#lark-cli-binary').value.trim(),
      lark_profile: $('#lark-profile').value.trim(),
      support_chat_id: $('#feishu-chat-id').value.trim(),
      base_app_token: $('#base-app-token').value.trim(),
      base_table_id: $('#base-table-id').value.trim(),
    };
    const appSecret = $('#feishu-app-secret').value.trim();
    if (appSecret) feishu.app_secret = appSecret;
    const management = {};
    const managementPassword = $('#management-password-setting').value.trim();
    if (managementPassword) management.password = managementPassword;
    return {
      version: 1,
      business: {
        repositories,
        ros_topology_file: $('#ros-topology-file').value.trim(),
        log_paths: $('#business-log-paths').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      },
      codex: {
        binary: $('#codex-binary').value.trim(),
        home: $('#codex-home').value.trim(),
        model: $('#codex-model').value.trim(),
        reasoning_effort: $('#codex-effort').value,
        timeout_seconds: Number($('#codex-timeout').value),
      },
      feishu,
      management,
    };
  }

  function setSettingsResult(message, error = false, success = false) {
    const result = $('#settings-result');
    result.textContent = message;
    result.className = `settings-result${error ? ' is-error' : ''}${success ? ' is-success' : ''}`;
  }

  async function saveSettings(event) {
    event.preventDefault();
    setSettingsResult('正在保存');
    try {
      const settings = await api.saveSettings(collectSettings());
      renderSettings(settings);
      setSettingsResult('设置已保存并生效', false, true);
    } catch (error) {
      setSettingsResult(error.message, true);
    }
  }

  async function verifyFeishu() {
    setSettingsResult('正在验证飞书连接');
    try {
      const mode = $('#feishu-connection-mode').value;
      await api.verifyFeishu(mode === 'lark_cli_profile' ? {
        connection_mode: mode,
        lark_cli_binary: $('#lark-cli-binary').value.trim(),
        lark_profile: $('#lark-profile').value.trim(),
      } : {
        connection_mode: mode,
        app_id: $('#feishu-app-id').value.trim(),
        app_secret: $('#feishu-app-secret').value.trim(),
      });
      setSettingsResult('飞书连接有效', false, true);
    } catch (error) {
      setSettingsResult(error.message, true);
    }
  }

  function issueRow(issue, management = false) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'issue-row';
    button.dataset.issueId = issue.id;
    button.innerHTML = `
      <strong>${escapeHtml(issue.id)}</strong>
      <span class="status-badge status-${issue.status}">${STATUS_LABELS[issue.status]}</span>
      <p class="summary">${escapeHtml(issue.summary)}</p>
      <span class="issue-meta">${escapeHtml(issue.date)}</span>`;
    button.addEventListener('click', () => openIssueDetail(issue));
    if (!management) return button;

    const row = document.createElement('div');
    row.className = 'managed-issue-row';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'issue-select';
    checkbox.checked = state.selectedIssueIds.has(issue.id);
    checkbox.setAttribute('aria-label', `选择问题 ${issue.id}`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selectedIssueIds.add(issue.id);
      else state.selectedIssueIds.delete(issue.id);
      updateManagementToolbar();
    });
    row.append(checkbox, button);
    return row;
  }

  function renderIssueLists() {
    const recent = $('#recent-list');
    recent.replaceChildren(...state.issues.slice(0, 2).map((issue) => issueRow(issue)));
  }

  function renderHistory() {
    const term = $('#history-search').value.trim().toLowerCase();
    const visible = state.issues.filter((issue) => {
      const matchesStatus = state.filter === 'all' || issue.status === state.filter;
      const matchesTerm = !term || `${issue.id} ${issue.summary}`.toLowerCase().includes(term);
      return matchesStatus && matchesTerm;
    });
    const list = $('#history-list');
    $('#management-toolbar').classList.toggle('is-hidden', !state.managementMode);
    $('#management-mode-button').textContent = state.managementMode ? '退出管理' : '管理';
    $('#history-screen').classList.toggle('is-management', state.managementMode);
    updateManagementToolbar();
    if (!visible.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-list';
      empty.textContent = '没有找到相关问题';
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...visible.map((issue) => issueRow(issue, state.managementMode)));
  }

  function updateManagementToolbar() {
    const count = state.selectedIssueIds.size;
    $('#management-selection-count').textContent = `已选择 ${count} 条`;
    $('#delete-selected-button').disabled = count === 0;
  }

  async function toggleManagementMode() {
    if (state.managementMode) {
      const token = state.managementToken;
      state.managementMode = false;
      state.managementToken = '';
      state.selectedIssueIds.clear();
      renderHistory();
      try {
        await api.lockManagement(token);
      } catch (_) { /* the local session may already have expired */ }
      return;
    }
    const dialog = $('#management-dialog');
    $('#management-password').value = '';
    $('#management-error').textContent = '';
    try {
      const status = await api.managementStatus();
      state.managementConfigured = Boolean(status.configured);
      if (!status.configured) {
        window.alert('请先在设置页配置工程模式密码');
        navigate('settings');
        return;
      }
      $('#management-dialog-title').textContent = '输入管理密码';
      $('#management-dialog-copy').textContent = '验证成功后进入工程模式。';
      $('#management-password').autocomplete = 'current-password';
      dialog.showModal();
      $('#management-password').focus();
    } catch (error) {
      window.alert(error.message);
    }
  }

  async function unlockManagement(event) {
    event.preventDefault();
    const password = $('#management-password').value;
    $('#management-error').textContent = '';
    try {
      const result = await api.unlockManagement(password);
      state.managementToken = result.management_token;
      state.managementMode = true;
      state.selectedIssueIds.clear();
      $('#management-dialog').close();
      renderHistory();
    } catch (error) {
      $('#management-error').textContent = error.message;
    }
  }

  function showDeleteDialog() {
    const selected = [...state.selectedIssueIds];
    if (!selected.length) return;
    const selectedRoots = selected.filter((issueId) => {
      const issue = state.issues.find((candidate) => candidate.id === issueId);
      return issue && issue.id === issue.rootId;
    });
    const extra = selectedRoots.length
      ? ' 其中包含主问题，关联的复发记录也会一起删除。'
      : '';
    $('#delete-dialog-copy').textContent = `已选择 ${selected.length} 条问题记录。${extra} 同时删除关联的 Codex 会话。若与其他记录共用会话，其余本地记录保留，下次分析时重建对话。`;
    $('#delete-error').textContent = '';
    $('#delete-dialog').showModal();
  }

  async function deleteSelectedIssues(event) {
    event.preventDefault();
    const selected = [...state.selectedIssueIds];
    if (!selected.length) return;
    const confirmButton = $('#confirm-delete');
    confirmButton.disabled = true;
    confirmButton.textContent = '正在删除';
    $('#delete-error').textContent = '';
    try {
      const result = await api.deleteIssues(selected, state.managementToken);
      const deleted = new Set(result.deleted_issue_ids || selected);
      state.issues = state.issues.filter((issue) => !deleted.has(issue.id));
      deleted.forEach((issueId) => state.selectedIssueIds.delete(issueId));
      saveIssues();
      $('#delete-dialog').close();
      renderHistory();
      renderIssueLists();
    } catch (error) {
      $('#delete-error').textContent = error.message;
      if (/管理模式/.test(error.message)) {
        state.managementMode = false;
        state.managementToken = '';
        state.selectedIssueIds.clear();
      }
    } finally {
      confirmButton.disabled = false;
      confirmButton.textContent = '确认删除';
    }
  }

  function demoTimeline(issue) {
    const messages = [
      { role: 'reporter', content: issue.summary, created_at: issue.date },
    ];
    if (issue.status !== 'open') {
      messages.push({ role: 'assistant', content: '现场信息已完成分析，并给出后续处理建议。', created_at: issue.date });
    }
    const solutions = issue.status === 'open' ? [] : [{
      version: issue.latestSolutionVersion || 1,
      content: issue.solution || '重新连接设备数据线，确认接口固定后重启业务程序。',
      verification_method: '确认设备状态正常，并完成一次原操作流程。',
    }];
    return { messages, solutions };
  }

  async function openIssueDetail(issue) {
    if (issue.status !== 'closed') {
      await resumeIssueChat(issue);
      return;
    }
    state.detailOrigin = state.screen === 'history' ? 'history' : 'init';
    state.detailIssue = issue;
    state.detailTimeline = null;
    state.detailError = '';
    navigate('detail');
    try {
      const timeline = await api.timeline(issue.id);
      if (state.detailIssue?.id !== issue.id) return;
      state.detailTimeline = timeline || demoTimeline(issue);
    } catch (error) {
      if (state.detailIssue?.id !== issue.id) return;
      state.detailError = error.message;
      state.detailTimeline = demoTimeline(issue);
    }
    renderIssueDetail();
  }

  async function resumeIssueChat(issue) {
    state.chatOrigin = state.screen === 'history' ? 'history' : 'init';
    state.current = { ...issue };
    state.messages = [];
    state.analysisProgress = null;
    state.phase = 'analyzing';
    navigate('chat');
    try {
      const timeline = await api.timeline(issue.id) || demoTimeline(issue);
      const remote = timeline.issue || {};
      state.current = {
        ...state.current,
        status: remote.status || issue.status,
        handoffState: remote.handoff_state || issue.handoffState || 'none',
        latestSolutionVersion: remote.latest_solution_version || issue.latestSolutionVersion || 0,
      };
      state.messages = (timeline.messages || []).map((message) => ({
        role: message.role === 'reporter' ? 'user' : 'assistant',
        content: message.content,
      }));
      const timelineMessages = timeline.messages || [];
      const solutions = timeline.solutions || [];
      const latestSolution = solutions.length ? solutions[solutions.length - 1] : null;
      if (state.current.status === 'pending_verification' && latestSolution) {
        state.current.latestSolutionVersion = latestSolution.version;
        state.current.solution = latestSolution.content;
        state.current.verificationMethod = latestSolution.verification_method || '';
        state.phase = 'pending';
      } else if (state.current.handoffState !== 'none') {
        state.phase = 'human';
      } else if (timelineMessages.length && timelineMessages[timelineMessages.length - 1].role === 'reporter' && !api.demo) {
        state.phase = 'analyzing';
      } else if (timelineMessages.some((message) => message.role === 'assistant')) {
        state.phase = 'result';
      } else {
        state.phase = 'ready';
      }
      if (state.phase === 'analyzing') state.messages.push({ role: 'assistant', typing: true });
      renderChat();
      if (state.phase === 'human') pollHumanResult(issue.id);
      if (state.phase === 'analyzing') {
        const assistantCount = (timeline.messages || []).filter((message) => message.role === 'assistant').length;
        pollTimeline(issue.id, assistantCount);
      }
    } catch (error) {
      state.messages = [{ role: 'assistant', content: '问题记录暂时无法读取，请稍后重试。' }];
      state.phase = 'result';
      renderChat();
    }
  }

  function formatDetailDate(value, fallback = '') {
    if (!value) return fallback;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
  }

  function renderIssueDetail() {
    const issue = state.detailIssue;
    if (!issue) return;
    $('#detail-id').textContent = issue.id;
    $('#detail-date').textContent = issue.date || '问题详情';
    const badge = $('#detail-status');
    badge.textContent = STATUS_LABELS[issue.status] || issue.status;
    badge.className = `status-badge status-${issue.status}`;

    const timeline = state.detailTimeline;
    $('#detail-description').textContent = timeline
      ? (timeline.messages || []).find((message) => message.role === 'reporter')?.content || issue.summary
      : '正在读取问题记录…';

    const timelineContainer = $('#detail-timeline');
    if (!timeline) {
      timelineContainer.innerHTML = '<p class="detail-empty">正在读取处理记录…</p>';
    } else {
      const roleLabels = { reporter: '现场人员', assistant: 'Codex', engineer: '工程师', system: '系统' };
      const entries = (timeline.messages || []).map((message) => {
        const item = document.createElement('article');
        item.className = 'detail-message';
        const heading = document.createElement('div');
        heading.className = 'detail-message-heading';
        const role = document.createElement('strong');
        role.textContent = roleLabels[message.role] || message.role || '记录';
        const date = document.createElement('span');
        date.textContent = formatDetailDate(message.created_at);
        heading.append(role, date);
        const content = document.createElement('p');
        content.textContent = message.content;
        item.append(heading, content);
        return item;
      });
      if (state.detailError) {
        const warning = document.createElement('p');
        warning.className = 'detail-notice';
        warning.textContent = '完整记录暂时无法读取，已显示本地保存的信息。';
        entries.unshift(warning);
      }
      if (!entries.length) {
        const empty = document.createElement('p');
        empty.className = 'detail-empty';
        empty.textContent = '暂无处理记录';
        entries.push(empty);
      }
      timelineContainer.replaceChildren(...entries);
    }

    const latestSolution = timeline?.solutions?.length
      ? timeline.solutions[timeline.solutions.length - 1]
      : null;
    $('#detail-solution').textContent = timeline
      ? (latestSolution?.content || '尚未提交解决方案')
      : '正在读取解决方案…';
    const verificationBlock = $('#detail-verification-block');
    verificationBlock.classList.toggle('is-hidden', !latestSolution?.verification_method);
    $('#detail-verification').textContent = latestSolution?.verification_method || '';
    const reopenButton = $('#detail-reopen-button');
    reopenButton.classList.toggle('is-hidden', issue.status !== 'closed');
    reopenButton.disabled = !timeline || issue.status !== 'closed';
  }

  function showReopenDialog(issue) {
    state.selectedForReopen = issue;
    $('#reopen-copy').textContent = `“${issue.summary}”再次发生后，将保留原记录并建立独立的本次记录。`;
    $('#next-sub-id').textContent = nextSubId(issue);
    $('#reopen-dialog').showModal();
  }

  async function startNewIssue() {
    const created = await api.createIssue();
    state.chatOrigin = 'init';
    state.current = { ...created, rootId: created.rootId || created.id, date: '刚刚', summary: '等待描述问题', nextSub: 1 };
    state.phase = 'ready';
    state.messages = [];
    navigate('chat');
    messageInput.focus();
  }

  async function confirmReopen(event) {
    if (!state.selectedForReopen) return;
    event.preventDefault();
    const parent = state.selectedForReopen;
    const predictedId = nextSubId(parent);
    const created = await api.createSubIssue(parent.rootId || parent.id);
    const id = created?.id || predictedId;
    parent.nextSub = (parent.nextSub || 1) + 1;
    parent.status = 'open';
    state.current = { id, rootId: parent.rootId || parent.id, parentId: parent.id, status: 'open', date: '刚刚', summary: '等待描述本次现象', nextSub: parent.nextSub };
    state.chatOrigin = state.detailOrigin;
    state.phase = 'ready';
    state.messages = [];
    saveIssues();
    $('#reopen-dialog').close();
    navigate('chat');
    messageInput.focus();
  }

  function renderChat() {
    if (!state.current) return;
    $('#issue-id').textContent = state.current.id;
    $('#issue-date').textContent = state.current.date || '今天';
    const badge = $('#issue-status');
    badge.textContent = STATUS_LABELS[state.current.status];
    badge.className = `status-badge status-${state.current.status}`;
    const notice = $('#parent-notice');
    notice.classList.toggle('is-hidden', !state.current.parentId);
    if (state.current.parentId) notice.textContent = `这是 ${state.current.rootId} 的再次上报，本次记录独立保存。`;
    renderMessages();
    renderWorkflow();
  }

  function renderMessages(preserveScroll = false) {
    const container = $('#messages');
    const chatScroll = $('#chat-scroll');
    const previousTop = chatScroll.scrollTop;
    const followBottom = !preserveScroll || chatScroll.scrollHeight - previousTop - chatScroll.clientHeight < 80;
    if (!state.messages.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-chat';
      empty.innerHTML = '<div><span class="empty-icon" aria-hidden="true">?</span><h3>请描述现在看到的现象</h3><p>无需判断原因，直接说设备或软件出现了什么情况。</p></div>';
      container.replaceChildren(empty);
      return;
    }
    container.replaceChildren(...state.messages.map((message) => {
      const item = document.createElement('article');
      item.className = `message ${message.role}`;
      const label = document.createElement('p');
      label.className = 'message-label';
      label.textContent = message.role === 'user' ? '现场' : '调试助手';
      const bubble = document.createElement('div');
      bubble.className = 'message-bubble';
      if (!message.typing) {
        const content = document.createElement('div');
        content.textContent = message.content;
        bubble.append(content);
      }
      if (message.typing || message.streaming) {
        const status = message.progress || analysisStatus(state.analysisProgress);
        bubble.setAttribute('aria-busy', 'true');
        const progress = document.createElement('div');
        progress.className = `analysis-progress${message.streaming ? ' streaming' : ''}${status.waiting ? ' is-waiting' : ''}`;
        const title = document.createElement('p');
        title.className = 'analysis-stage';
        title.textContent = status.prompt;
        const timing = document.createElement('p');
        timing.className = 'analysis-timing';
        timing.textContent = status.hint;
        progress.append(title, timing);
        bubble.append(progress);
      }
      item.append(label, bubble);
      return item;
    }));
    chatScroll.scrollTop = followBottom ? chatScroll.scrollHeight : previousTop;
  }

  function renderWorkflow() {
    const locked = ['analyzing', 'human', 'pending', 'failure_report', 'closed'].includes(state.phase);
    composer.classList.toggle('is-hidden', state.phase === 'closed');
    messageInput.disabled = locked;
    composer.querySelector('.send-button').disabled = locked;
    const inputPrompts = {
      analyzing: '回复完成后可继续补充现象',
      human: state.current?.handoffState === 'delivered'
        ? '工程师正在处理中，暂时无法输入'
        : '正在联系工程师，暂时无法输入',
      pending: '请先按当前方案验证并确认结果',
      failure_report: '请在上方填写验证失败现象',
    };
    messageInput.placeholder = inputPrompts[state.phase] || '例如：设备指示灯亮，但操作没有反应';
    if (state.phase !== 'analyzing') state.analysisProgress = null;
    workflowPanel.className = 'workflow-panel';

    if (state.phase === 'ready') {
      workflowPanel.classList.add('is-hidden');
    } else if (state.phase === 'analyzing') {
      workflowPanel.classList.add('is-hidden');
      workflowPanel.innerHTML = '';
    } else if (state.phase === 'result') {
      workflowPanel.innerHTML = '<h3>问题是否已经解决？</h3><p>如果问题仍存在，可以继续补充现象或转给工程师处理。</p><div class="panel-actions"><button class="secondary-button" id="ai-solved" type="button">已解决</button><button class="primary-button" id="handoff" type="button">转人工处理</button></div>';
      $('#ai-solved').addEventListener('click', confirmAiSolved);
      $('#handoff').addEventListener('click', handoffToHuman);
    } else if (state.phase === 'human') {
      workflowPanel.classList.add('is-hidden');
      workflowPanel.innerHTML = '';
    } else if (state.phase === 'feishu_setup') {
      workflowPanel.classList.add('human');
      workflowPanel.innerHTML = '<h3>飞书尚未配置</h3><p>问题仍保存在本机。完成飞书连接和工程师支持群设置后，再次点击转人工处理。</p><div class="panel-actions"><button class="primary-button" id="open-feishu-settings" type="button">打开飞书设置</button></div>';
      $('#open-feishu-settings').addEventListener('click', () => navigate('settings'));
    } else if (state.phase === 'handoff_error') {
      workflowPanel.classList.add('human');
      workflowPanel.innerHTML = `<h3>发送到飞书失败</h3><p>${escapeHtml(state.handoffError || '请检查飞书连接后重试。')}</p><div class="panel-actions"><button class="secondary-button" id="retry-handoff" type="button">重试</button><button class="primary-button" id="open-feishu-settings" type="button">打开设置</button></div>`;
      $('#retry-handoff').addEventListener('click', handoffToHuman);
      $('#open-feishu-settings').addEventListener('click', () => navigate('settings'));
    } else if (state.phase === 'pending') {
      workflowPanel.classList.add('solution');
      const solution = state.current.solution || '重新连接设备数据线，确认接口固定后重启业务程序。';
      const verificationMethod = state.current.verificationMethod || '按上述方法操作后，确认问题是否恢复。';
      workflowPanel.innerHTML = `<h3>方案待验证</h3><p>当前建议的处理方法：</p><p class="solution-body">${escapeHtml(solution)}</p><p class="verification-label">现场验证方法：</p><p class="solution-body verification-body">${escapeHtml(verificationMethod)}</p><div class="panel-actions"><button class="primary-button" id="confirm-solved" type="button">已解决</button><button class="secondary-button" id="report-unsolved" type="button">未解决</button></div>`;
      $('#confirm-solved').addEventListener('click', confirmSolved);
      $('#report-unsolved').addEventListener('click', () => { state.phase = 'failure_report'; renderChat(); });
    } else if (state.phase === 'failure_report') {
      workflowPanel.classList.add('human');
      workflowPanel.innerHTML = '<h3>填写未解决现象</h3><p>说明按方案操作后的结果，工程师会据此提交下一版解决方案。</p><textarea id="verification-failure" rows="3" maxlength="2000" placeholder="例如：重新插线并重启后，左侧相机仍显示设备未找到"></textarea><p class="form-error is-hidden" id="verification-failure-error"></p><div class="panel-actions"><button class="secondary-button" id="cancel-failure" type="button">返回验证</button><button class="primary-button" id="submit-failure" type="button">提交未解决</button></div>';
      $('#cancel-failure').addEventListener('click', () => { state.phase = 'pending'; renderChat(); });
      $('#submit-failure').addEventListener('click', submitVerificationFailure);
    } else if (state.phase === 'closed') {
      workflowPanel.classList.add('solution');
      workflowPanel.innerHTML = '<h3>问题已解决</h3><p>本次问题、现场状态和解决方案均已保存。</p><div class="panel-actions"><button class="primary-button" id="finish" type="button">完成</button></div>';
      $('#finish').addEventListener('click', () => navigate('init'));
    }
  }

  async function submitMessage(event) {
    event.preventDefault();
    const content = messageInput.value.trim();
    if (!content || !state.current || !['ready', 'result'].includes(state.phase)) return;
    const previousAssistantCount = state.messages.filter((message) => message.role === 'assistant' && !message.typing && !message.queued && !message.streaming).length;
    state.messages.push({ role: 'user', content });
    state.current.summary = content.slice(0, 48);
    if (!state.issues.some((issue) => issue.id === state.current.id)) state.issues.unshift(state.current);
    saveIssues();
    messageInput.value = '';
    state.analysisProgress = null;
    state.phase = 'analyzing';
    state.messages.push({ role: 'assistant', typing: true });
    renderChat();
    try {
      await api.appendMessage(state.current.id, content);
      if (!api.demo) {
        await pollTimeline(state.current.id, previousAssistantCount);
        return;
      }
    } catch (error) {
      state.messages = state.messages.filter((message) => !message.typing);
      state.messages.push({ role: 'assistant', content: '现场信息提交失败，请检查本机服务后重新发送。' });
      state.phase = 'result';
      renderChat();
      return;
    }
    window.setTimeout(() => {
      state.messages = state.messages.filter((message) => !message.typing);
      state.messages.push({ role: 'assistant', content: '已保存现场状态。目前发现业务进程仍在运行，但最近日志中出现了设备连接失败。请先确认设备连接线是否松动；如果连接正常，可以转给工程师进一步处理。' });
      state.phase = 'result';
      renderChat();
    }, 900);
  }

  function submitComposerOnEnter(event) {
    if (
      event.key !== 'Enter'
      || event.shiftKey
      || event.isComposing
      || event.keyCode === 229
    ) return;
    event.preventDefault();
    if (messageInput.disabled) return;
    composer.requestSubmit();
  }

  async function handoffToHuman() {
    try {
      const settings = await api.getSettings();
      const feishu = settings.feishu || {};
      const credentialsReady = feishu.connection_mode !== 'lark_cli_profile' && feishu.app_id && feishu.app_secret_configured;
      const profileReady = feishu.connection_mode === 'lark_cli_profile' && feishu.lark_cli_binary && feishu.lark_profile;
      if (!(credentialsReady || profileReady) || !feishu.support_chat_id) {
        state.phase = 'feishu_setup';
        renderChat();
        return;
      }
      const issue = await api.requestHandoff(state.current.id);
      if (issue) state.current = { ...state.current, ...issue };
      state.phase = 'human';
      renderChat();
    } catch (error) {
      state.handoffError = error.message;
      state.phase = 'handoff_error';
      renderChat();
      return;
    }
    if (api.demo) {
      window.setTimeout(() => { state.current.status = 'pending_verification'; state.phase = 'pending'; renderChat(); }, 1300);
    } else {
      pollHumanResult(state.current.id);
    }
  }

  async function confirmSolved() {
    await api.confirmSolution(state.current.id, state.current.latestSolutionVersion || 1);
    state.current.status = 'closed';
    state.phase = 'closed';
    const stored = state.issues.find((issue) => issue.id === state.current.id);
    if (stored) stored.status = 'closed';
    saveIssues();
    renderChat();
  }

  async function submitVerificationFailure() {
    const observation = $('#verification-failure').value.trim();
    const error = $('#verification-failure-error');
    if (!observation) {
      error.textContent = '请描述按方案操作后仍然存在的现象。';
      error.classList.remove('is-hidden');
      return;
    }
    const button = $('#submit-failure');
    button.disabled = true;
    try {
      const issue = await api.reportVerificationFailure(state.current.id, observation);
      state.current = { ...state.current, ...issue, status: 'open', solution: null, verificationMethod: null };
      const stored = state.issues.find((item) => item.id === state.current.id);
      if (stored) stored.status = 'open';
      saveIssues();
      state.phase = 'human';
      renderChat();
      if (!api.demo) pollHumanResult(state.current.id);
    } catch (requestError) {
      error.textContent = `提交失败：${requestError.message}`;
      error.classList.remove('is-hidden');
      button.disabled = false;
    }
  }

  async function confirmAiSolved() {
    const issue = await api.confirmAiResolution(state.current.id);
    state.current = { ...state.current, ...issue, status: 'closed' };
    state.phase = 'closed';
    const stored = state.issues.find((item) => item.id === state.current.id);
    if (stored) stored.status = 'closed';
    saveIssues();
    renderChat();
  }

  let analysisPollVersion = 0;

  function analysisStatus(analysis, connectionLost = false) {
    const stages = {
      capturing: '正在收集现场状态', preparing: '正在准备分析', starting: '正在启动 Codex',
      waiting: '等待 Codex 响应', analyzing: '正在分析现场信息', checking: '正在检查现场信息',
      reading_logs: '正在读取日志', reading_code: '正在检查业务代码', responding: '正在生成回复',
      saving: '正在保存分析结果', retrying: '服务响应异常，Codex 正在重试', limited: 'AI 正在处理，请稍候',
    };
    if (connectionLost) return {
      prompt: '暂时无法连接本机服务，正在重试',
      hint: '进度暂时无法更新，请确认现场助手仍在运行。', waiting: true,
    };
    if (!analysis) return {
      prompt: 'AI 正在分析，请稍候', hint: '等待任务开始，现场信息提交后会自动保存。', waiting: false,
    };
    const elapsed = Math.max(0, Math.floor(Number(analysis.elapsed_seconds) || 0));
    const idle = Math.max(0, Math.floor(Number(analysis.idle_seconds) || 0));
    const stage = stages[analysis.stage] || 'AI 正在分析';
    if (analysis.stage === 'limited') return {
      prompt: stage, hint: `已用 ${elapsed} 秒 · 当前连接不提供中间进度，完成后会显示结果。`, waiting: false,
    };
    if (idle >= 20) {
      const localStep = ['capturing', 'preparing', 'saving'].includes(analysis.stage);
      return {
        prompt: localStep ? '等待当前步骤完成，暂时没有新进展' : '等待 Codex 响应，暂时没有新进展',
        hint: `已用 ${elapsed} 秒 · ${idle} 秒未收到新进展。${localStep ? '现场信息仍在本机处理。' : '可能仍在处理，或网络／服务响应较慢。'}`,
        waiting: true,
      };
    }
    return {
      prompt: stage,
      hint: `已用 ${elapsed} 秒 · ${idle < 2 ? '刚刚收到新进展' : `${idle} 秒前收到新进展`}`,
      waiting: analysis.stage === 'retrying',
    };
  }

  function renderAnalysisProgress(analysis, connectionLost = false) {
    if (state.phase !== 'analyzing') return;
    const status = analysisStatus(analysis, connectionLost);
    const message = state.messages.find((item) => item.typing || item.streaming);
    if (message && JSON.stringify(message.progress) === JSON.stringify(status)) return;
    if (message) message.progress = status;
    else {
      state.messages = state.messages.filter((item) => !item.queued);
      state.messages.push({ role: 'assistant', typing: true, progress: status });
    }
    renderMessages(true);
  }

  function showStreamingReply(analysis) {
    if (!analysis?.content) {
      renderAnalysisProgress(analysis);
      return;
    }
    const existing = state.messages.find((message) => message.streaming);
    if (existing?.content === analysis.content) {
      renderAnalysisProgress(analysis);
      return;
    }
    state.messages = state.messages.filter((message) => !message.typing && !message.streaming && !message.queued);
    state.messages.push({ role: 'assistant', content: analysis.content, streaming: true, progress: analysisStatus(analysis) });
    // Keep the composer locked until the completed reply is persisted.
    renderMessages(true);
  }

  async function pollTimeline(issueId, previousAssistantCount = 0) {
    const version = ++analysisPollVersion;
    const active = () => version === analysisPollVersion && state.current?.id === issueId && state.phase === 'analyzing';
    for (let attempt = 0; attempt < 300 && active(); attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 200));
      if (!active()) return;
      try {
        const timeline = await api.timeline(issueId);
        if (!active()) return;
        const messages = timeline.messages || [];
        const assistants = messages.filter((message) => message.role === 'assistant');
        const assistant = assistants.length > previousAssistantCount ? assistants[assistants.length - 1] : null;
        if (assistant) {
          state.messages = state.messages.filter((message) => !message.typing && !message.streaming && !message.queued);
          state.messages.push({ role: 'assistant', content: assistant.content });
          state.phase = 'result';
          renderChat();
          return;
        }
        if (timeline.analysis?.status === 'running') {
          attempt = 0;
          state.analysisProgress = timeline.analysis;
          showStreamingReply(timeline.analysis);
        } else {
          renderAnalysisProgress(null);
        }
        const issue = timeline.issue;
        if (issue.status === 'pending_verification' && timeline.solutions?.length) {
          const solution = timeline.solutions[timeline.solutions.length - 1];
          state.current.status = issue.status;
          state.current.latestSolutionVersion = solution.version;
          state.current.solution = solution.content;
          state.current.verificationMethod = solution.verification_method || '';
          state.phase = 'pending';
          renderChat();
          return;
        }
      } catch (error) {
        if (!active()) return;
        renderAnalysisProgress(state.analysisProgress, true);
        console.warn('timeline polling failed', error);
      }
    }
    if (!active()) return;
    state.messages = state.messages.filter((message) => !message.typing && !message.streaming);
    state.messages.push({ role: 'assistant', queued: true, content: '现场信息已保存，当前分析仍在排队。网络恢复后会继续处理，也可以直接转给工程师。' });
    state.phase = 'result';
    renderChat();
    pollQueuedAnalysis(issueId, previousAssistantCount);
  }

  async function pollQueuedAnalysis(issueId, previousAssistantCount) {
    for (let attempt = 0; attempt < 720 && state.current?.id === issueId && state.phase === 'result'; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 5000));
      try {
        const timeline = await api.timeline(issueId);
        if (state.current?.id !== issueId || state.phase !== 'result') return;
        const assistants = (timeline.messages || []).filter((message) => message.role === 'assistant');
        if (assistants.length > previousAssistantCount) {
          const assistant = assistants[assistants.length - 1];
          state.messages = state.messages.filter((message) => !message.queued && !message.typing && !message.streaming);
          state.messages.push({ role: 'assistant', content: assistant.content });
          renderChat();
          return;
        }
        if (timeline.analysis?.status === 'running') {
          state.phase = 'analyzing';
          state.analysisProgress = timeline.analysis;
          showStreamingReply(timeline.analysis);
          renderWorkflow();
          pollTimeline(issueId, previousAssistantCount);
          return;
        }
      } catch (error) {
        console.warn('queued analysis polling failed', error);
      }
    }
  }

  async function pollHumanResult(issueId) {
    while (state.current?.id === issueId && state.phase === 'human') {
      await new Promise((resolve) => window.setTimeout(resolve, 2000));
      try {
        const timeline = await api.timeline(issueId);
        const issue = timeline.issue;
        state.current.handoffState = issue.handoff_state || state.current.handoffState;
        if (issue.status === 'pending_verification' && timeline.solutions?.length) {
          const solution = timeline.solutions[timeline.solutions.length - 1];
          state.current.status = issue.status;
          state.current.latestSolutionVersion = solution.version;
          state.current.solution = solution.content;
          state.current.verificationMethod = solution.verification_method || '';
          state.phase = 'pending';
          renderChat();
          return;
        }
        renderWorkflow();
      } catch (error) {
        console.warn('human result polling failed', error);
      }
    }
  }

  function escapeHtml(value) {
    const element = document.createElement('span');
    element.textContent = value;
    return element.innerHTML;
  }

  $('#new-issue-button').addEventListener('click', startNewIssue);
  $('#view-all-button').addEventListener('click', () => navigate('history'));
  $('#settings-button').addEventListener('click', () => navigate('settings'));
  backButton.addEventListener('click', () => {
    if (state.screen === 'detail') navigate(state.detailOrigin);
    else if (state.screen === 'chat') navigate(state.chatOrigin);
    else navigate('init');
  });
  $('#minimize-button').addEventListener('click', () => { if (window.desktopBridge?.hideChat) window.desktopBridge.hideChat(); else window.close(); });
  $('#history-search').addEventListener('input', renderHistory);
  $('#management-mode-button').addEventListener('click', toggleManagementMode);
  $('#management-form').addEventListener('submit', unlockManagement);
  $('#cancel-management').addEventListener('click', () => $('#management-dialog').close());
  $('#delete-selected-button').addEventListener('click', showDeleteDialog);
  $('#delete-form').addEventListener('submit', deleteSelectedIssues);
  $('#cancel-delete').addEventListener('click', () => $('#delete-dialog').close());
  $$('.filter-tab').forEach((tab) => tab.addEventListener('click', () => {
    state.filter = tab.dataset.filter;
    $$('.filter-tab').forEach((candidate) => candidate.classList.toggle('is-active', candidate === tab));
    renderHistory();
  }));
  $('#confirm-reopen').addEventListener('click', confirmReopen);
  $('#detail-reopen-button').addEventListener('click', () => {
    if (state.detailIssue?.status === 'closed') showReopenDialog(state.detailIssue);
  });
  $('#add-repository').addEventListener('click', () => addRepositoryRow());
  $('#feishu-connection-mode').addEventListener('change', updateFeishuMode);
  $('#verify-feishu').addEventListener('click', verifyFeishu);
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#startup-enabled').addEventListener('change', changeStartup);
  composer.addEventListener('submit', submitMessage);
  messageInput.addEventListener('keydown', submitComposerOnEnter);

  if (api.demo) {
    renderIssueLists();
    loadBusinessStatus();
  } else {
    api.listIssues().then((issues) => {
      state.issues = issues;
      renderIssueLists();
    }).catch(() => renderIssueLists());
    loadBusinessStatus();
  }
})();
