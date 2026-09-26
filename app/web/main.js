'use strict';
const $ = (selector) => document.querySelector(selector);
let status = null;
let paused = false;
let logText = '';
let requestPending = false;
let activeTab = 'home';
let account = null;
let accountPending = false;

function selectTab(name) {
  activeTab = ['home', 'account', 'maintenance', 'logs'].includes(name) ? name : 'home';
  document.querySelectorAll('.tab').forEach(tab => { tab.hidden = tab.id !== activeTab; });
  document.querySelectorAll('.nav').forEach(nav => {
    const active = nav.dataset.tab === activeTab;
    nav.classList.toggle('active', active);
    if (active) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
  });
  if (activeTab === 'logs') pollLogs();
  if (activeTab === 'account') pollAccount();
}
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  location.hash = 'tab=' + button.dataset.tab;
}));
function route() {
  selectTab(new URLSearchParams(location.hash.slice(1)).get('tab'));
}
window.addEventListener('hashchange', route);

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Ошибка ${response.status}`);
  return data;
}

function buttons() {
  const busy = requestPending || accountPending || status?.job.status === 'running';
  $('#update').disabled = busy || !$('#package').files.length;
  $('#rollback').disabled = busy || !status?.previous;
  $('#package').disabled = busy;
  $('#repository-check').disabled = busy;
  $('#repository-install').disabled = busy || !status?.repository?.available;
  $('#account-login').disabled = busy;
  $('#account-check').disabled = busy || !account?.configured;
  $('#account-logout').disabled = busy || !account?.configured;
  $('#yandex-token').disabled = busy;
}

async function pollStatus() {
  try {
    status = await api('/api/status');
    $('#connection').textContent = status.healthy ? 'На связи' : 'Восстановление';
    $('#connection').className = `connection ${status.healthy ? 'online' : 'offline'}`;
    $('#version').textContent = status.version ? `v${status.version}` : 'Нет версии';
    $('#app-version').textContent = status.version || 'Не установлено';
    $('#service-version').textContent = status.maintenance_version;
    $('#runtime').textContent = `Python ${status.python}`;
    $('#app-health').textContent = status.healthy ? 'Приложение работает' : 'Приложение недоступно';
    $('#installed-commit').textContent = status.source?.commit ? `Установлена сборка ${status.source.commit.slice(0, 8)}` : 'Текущая версия установлена из локального пакета';
    const repository = status.repository;
    if (repository?.status === 'checked') {
      $('#repository-info').textContent = `${repository.available ? 'Доступна сборка' : 'Установлена актуальная сборка'} ${repository.commit.slice(0, 8)} — ${repository.message}`;
    } else if (repository?.status === 'error') {
      $('#repository-info').textContent = repository.message;
    }
    if (!requestPending) {
      $('#job').textContent = status.job.message;
      $('#job').className = `notice ${status.job.status}`;
    }
    buttons();
  } catch (error) {
    $('#connection').textContent = 'Нет связи';
    $('#connection').className = 'connection offline';
  }
}

async function operation(path, body) {
  requestPending = true;
  buttons();
  $('#job').className = 'notice';
  $('#job').textContent = 'Отправляем запрос…';
  try {
    const result = await api(path, { method: 'POST', headers: {'X-MediaStr-Request': '1', 'Content-Type': 'application/octet-stream'}, body });
    $('#job').textContent = result.message;
    $('#package').value = '';
    requestPending = false;
    await pollStatus();
  } catch (error) {
    $('#job').textContent = error.message;
    $('#job').className = 'notice error';
    requestPending = false;
  }
  buttons();
}

$('#package').addEventListener('change', buttons);
$('#repository-check').addEventListener('click', () => operation('/api/repository/check'));
$('#repository-install').addEventListener('click', () => {
  const revision = status?.repository;
  if (revision?.available && confirm(`Установить сборку ${revision.commit.slice(0, 8)} из GorINIch73/volumio_Ya?`)) {
    operation('/api/repository/install', JSON.stringify({commit: revision.commit}));
  }
});
$('#update').addEventListener('click', () => {
  const file = $('#package').files[0];
  if (!file) return;
  if (file.size > 16 * 1024 * 1024) {
    $('#job').textContent = 'Файл больше 16 МБ';
    $('#job').className = 'notice error';
    return;
  }
  if (confirm(`Установить ${file.name}? Устанавливайте только доверенные пакеты. Интерфейс может кратко перезапуститься.`)) operation('/api/update', file);
});
$('#rollback').addEventListener('click', () => {
  if (confirm('Вернуться к предыдущей версии приложения?')) operation('/api/rollback');
});

function renderLogs() {
  const level = $('#level').value;
  const lines = logText.split('\n');
  let matching = level === 'all';
  const filtered = lines.filter(line => {
    const start = line.match(/^\d{4}-\d{2}-\d{2} .*? (INFO|WARNING|ERROR) /);
    if (start) matching = level === 'all' || start[1] === level;
    return matching;
  });
  $('#log-output').textContent = filtered.join('\n') || 'Нет записей';
  if (!paused) $('#log-output').scrollTop = $('#log-output').scrollHeight;
}

async function pollLogs() {
  if (paused || activeTab !== 'logs') return;
  try {
    logText = (await api('/api/logs')).text;
    renderLogs();
  } catch (error) {
    $('#log-message').textContent = 'Не удалось получить логи: ' + error.message;
  }
}

function renderAccount(value) {
  account = value;
  $('#account-name').textContent = account.configured ? (account.account?.display_name || 'Аккаунт подключён') : 'Аккаунт не подключён';
  const checked = account.checked_at ? new Date(account.checked_at).toLocaleString() : '';
  $('#account-details').textContent = account.configured ? `${account.account?.login || account.account?.uid || ''} · Токен проверен ${checked}` : 'Вставьте токен для входа в Яндекс Музыку.';
  buttons();
}

async function pollAccount() {
  if (accountPending) return;
  try {
    renderAccount(await api('/api/yandex/status'));
  } catch (error) {
    $('#account-message').textContent = error.message;
    $('#account-message').className = 'notice error';
  }
}

async function accountAction(action, token) {
  accountPending = true;
  buttons();
  $('#account-message').textContent = action === 'logout' ? 'Отключаем аккаунт…' : 'Проверяем аккаунт в Яндексе…';
  $('#account-message').className = 'notice';
  try {
    const value = await api(`/api/yandex/${action}`, {method: 'POST', headers: {'X-MediaStr-Request': '1', 'Content-Type': 'application/json'}, body: token === undefined ? undefined : JSON.stringify({token})});
    renderAccount(value);
    $('#yandex-token').value = '';
    $('#account-message').textContent = action === 'logout' ? 'Аккаунт отключён, токен удалён с устройства' : 'Аккаунт проверен, токен сохранён на устройстве';
    $('#account-message').className = 'notice success';
  } catch (error) {
    $('#account-message').textContent = error.message;
    $('#account-message').className = 'notice error';
  } finally {
    accountPending = false;
    buttons();
  }
}

$('#account-form').addEventListener('submit', event => {
  event.preventDefault();
  const token = $('#yandex-token').value.trim();
  if (token) accountAction('login', token);
});
$('#account-check').addEventListener('click', () => accountAction('check'));
$('#account-logout').addEventListener('click', () => {
  if (confirm('Удалить сохранённый токен Яндекса с этого устройства?')) accountAction('logout');
});

$('#pause').addEventListener('click', () => {
  paused = !paused;
  $('#pause').textContent = paused ? 'Продолжить' : 'Пауза';
  $('#pause').setAttribute('aria-pressed', String(paused));
  if (!paused) pollLogs();
});
$('#level').addEventListener('change', renderLogs);
$('#copy').addEventListener('click', async () => {
  try {
    if (!navigator.clipboard) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText($('#log-output').textContent);
    $('#log-message').textContent = 'Логи скопированы';
  } catch (error) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents($('#log-output'));
    selection.removeAllRanges();
    selection.addRange(range);
    $('#log-message').textContent = 'Текст выделен: скопируйте вручную или нажмите «Скачать».';
  }
});

async function poll() {
  await pollStatus();
  await pollLogs();
  if (activeTab === 'account') await pollAccount();
  setTimeout(poll, 2500);
}
route();
poll();
