'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Controller = require('../index');
const {Journal} = require('../lib/journal');
const Updater = require('../lib/updater');

function fixture() {
  const calls = {sources: [], removed: [], mpd: [], toast: [], logs: []};
  const router = {
    pluginManager: {getPlugin: () => ({
      sendMpdCommand: async command => { calls.mpd.push(command); },
      seek: async position => { calls.mpd.push(['seek', position]); }
    })},
    volumioAddToBrowseSources: data => calls.sources.push(data),
    volumioRemoveToBrowseSources: name => calls.removed.push(name),
    stateMachine: {
      setConsumeUpdateService: async (service, ignoreMetadata) => { calls.consume = service; calls.ignoreMetadata = ignoreMetadata; },
      next: async () => { assert.equal(calls.consume, undefined); calls.next = true; },
      previous: async () => { assert.equal(calls.consume, undefined); calls.previous = true; }
    },
    volumioGetState: () => ({service: 'mpd'}),
    volumioStop: async () => { calls.stopped = true; },
    pushToastMessage: (...data) => calls.toast.push(data)
  };
  const controller = new Controller({coreCommand: router, logger: {error: message => calls.logs.push(message)}});
  return {controller, calls, router};
}

const track = {id: '123', title: 'Песня', artist: 'Исполнитель', album: 'Альбом', duration: 100, available: true};
const stream = 'https://test.yandex.net/get-mp3/signature/123/file.mp3';

function fakeBackend(controller, request) {
  controller.started = true;
  controller.backend = {request, stop: async () => {}};
  controller.mpd = controller.commandRouter.pluginManager.getPlugin();
}

test('real worker: start, concurrent start, stop, restart; Kew contract', async () => {
  const {controller, calls, router} = fixture();
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-plugin-'));
  router.pluginManager.getConfigurationFile = () => path.join(data, 'config.json');
  try {
    const starting = controller.onStart();
    assert.equal(typeof starting.fail, 'function');
    await Promise.all([starting, controller.onStart()]);
    assert.equal(calls.sources.length, 1);
    const worker = controller.backend.process;
    assert.equal((await controller.handleBrowseUri('yam')).navigation.lists[0].items.length, 0);
    assert.equal((await controller.getUIConfig()).sections[0].content[0].value, '');
    await controller.onStop();
    assert.notEqual(worker.signalCode, null);
    await assert.rejects(Promise.resolve(controller.handleBrowseUri('yam')), /Включите/);
    await controller.onStart();
    assert.notEqual(controller.backend.process.pid, worker.pid);
    await controller.onStop();
  } finally {
    await controller.onStop();
    fs.rmSync(data, {recursive: true, force: true});
  }
});

test('stop during startup does not leave a source or worker', async () => {
  const {controller, calls, router} = fixture();
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-plugin-'));
  router.pluginManager.getConfigurationFile = () => path.join(data, 'config.json');
  const start = Promise.resolve(controller.onStart()).catch(() => {});
  await controller.onStop();
  await start;
  assert.equal(controller.backend, null);
  assert.equal(controller.started, false);
  assert.equal(calls.sources.length, 0);
  fs.rmSync(data, {recursive: true, force: true});
});

test('corrupt account permits startup and exposes a blank sign-in form', async () => {
  const {controller, router} = fixture();
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-plugin-'));
  fs.writeFileSync(path.join(data, 'yandex-account.json'), 'broken');
  router.pluginManager.getConfigurationFile = () => path.join(data, 'config.json');
  try {
    await controller.onStart();
    const ui = await controller.getUIConfig();
    assert.match(ui.sections[0].label, /Войдите заново/);
    assert.equal(ui.sections[0].content[0].value, '');
    await controller.logoutAccount();
    assert.equal((await controller.request('status')).configured, false);
  } finally {
    await controller.onStop();
    fs.rmSync(data, {recursive: true, force: true});
  }
});

test('crashed worker rejects requests and can be restarted', async () => {
  const {controller, calls, router} = fixture();
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-plugin-'));
  router.pluginManager.getConfigurationFile = () => path.join(data, 'config.json');
  try {
    await controller.onStart();
    const worker = controller.backend.process;
    await new Promise(resolve => { worker.once('exit', resolve); worker.kill('SIGKILL'); });
    assert.equal(controller.started, false);
    assert.equal(calls.logs.length, 1);
    await controller.onRestart();
    assert.equal(controller.started, true);
  } finally {
    await controller.onStop();
    fs.rmSync(data, {recursive: true, force: true});
  }
});

test('native Browse, pagination and unavailable tracks', async () => {
  const {controller} = fixture();
  fakeBackend(controller, async (method, params) => {
    if (method === 'status') return {configured: true};
    if (!params) return {playlists: [{kind: '42', title: 'Мой плейлист'}]};
    assert.equal(params.kind, 'likes');
    assert.equal(params.offset, 50);
    return {title: 'Мне нравится', tracks: [track, {...track, id: '124', available: false}], next_offset: 100};
  });
  const root = await controller.handleBrowseUri('yam');
  assert.equal(root.navigation.prev.uri, '/');
  assert.equal(root.navigation.lists[0].items[1].uri, 'yam/collection/42');
  const page = await controller.handleBrowseUri('yam/collection/likes/50');
  const items = page.navigation.lists[0].items;
  assert.equal(items[0].type, 'song');
  assert.equal(items[1].type, 'item-no-menu');
  assert.equal(items[1].uri, undefined);
  assert.equal(items[2].uri, 'yam/collection/likes/100');
  assert.equal(page.navigation.prev.uri, 'yam/collection/likes/0');
  await assert.rejects(Promise.resolve(controller.handleBrowseUri('other/collection/likes')), /Неизвестный/);
});

test('queue stores stable identifiers and resolves streams only at playback', async () => {
  const {controller, calls} = fixture();
  const methods = [];
  fakeBackend(controller, async (method, params) => {
    methods.push(method);
    assert.equal(params.id, '123');
    return method === 'stream' ? {uri: stream} : track;
  });
  const queued = await controller.explodeUri({uri: 'yam/track/123'});
  assert.deepEqual(methods, ['track']);
  assert.equal(queued[0].uri, 'yam/track/123');
  assert.equal(queued[0].service, 'yam');
  await controller.clearAddPlayTrack(queued[0]);
  await controller.clearAddPlayTrack(queued[0]);
  assert.deepEqual(methods, ['track', 'stream', 'stream']);
  assert.deepEqual(calls.mpd.slice(0, 4), ['stop', 'clear', 'add "' + stream + '"', 'play']);
  assert.equal(calls.consume, 'mpd');
  assert.equal(calls.ignoreMetadata, true);
  await assert.rejects(Promise.resolve(controller.explodeUri({uri: 'https://bad/'})), /Некорректный/);
});

test('resume restores MPD updates and skip controls select Volumio queue entries', async () => {
  const {controller, calls} = fixture();
  fakeBackend(controller, async () => ({}));
  await controller.resume();
  assert.equal(calls.consume, 'mpd');
  assert.equal(calls.ignoreMetadata, true);
  assert.deepEqual(calls.mpd, ['pause 0']);
  await controller.next();
  await controller.previous();
  assert.equal(calls.next, true);
  assert.equal(calls.previous, true);
});

test('stop cancels a slow stream request before MPD receives it', async () => {
  const {controller, calls} = fixture();
  let resolve;
  fakeBackend(controller, () => new Promise(done => { resolve = done; }));
  const playing = Promise.resolve(controller.clearAddPlayTrack({uri: 'yam/track/123'}));
  await controller.stop();
  resolve({uri: stream});
  await assert.rejects(playing);
  assert.deepEqual(calls.mpd, ['stop']);
  assert.deepEqual(calls.toast, []);
});

test('switching tracks discards an older late response', async () => {
  const {controller, calls} = fixture();
  let resolveOld;
  fakeBackend(controller, (method, params) => params.id === '123'
    ? new Promise(done => { resolveOld = done; }) : Promise.resolve({uri: stream}));
  const old = Promise.resolve(controller.clearAddPlayTrack({uri: 'yam/track/123'}));
  await controller.clearAddPlayTrack({uri: 'yam/track/124'});
  resolveOld({uri: stream});
  await assert.rejects(old);
  assert.equal(calls.mpd.filter(command => command === 'play').length, 1);
});

test('MPD failure does not disclose signed URLs', async () => {
  const {controller, calls} = fixture();
  fakeBackend(controller, async () => ({uri: stream}));
  controller.mpd.sendMpdCommand = async () => { throw new Error('Failed ' + stream); };
  await assert.rejects(Promise.resolve(controller.clearAddPlayTrack({uri: 'yam/track/123'})), /Не удалось/);
  assert.equal(JSON.stringify(calls).includes(stream), false);
});

test('resolved MPD error objects fail playback instead of reporting success', async () => {
  const {controller, calls} = fixture();
  fakeBackend(controller, async () => ({uri: stream}));
  controller.mpd.sendMpdCommand = async command => {
    calls.mpd.push(command);
    return command.startsWith('add ') ? {error: 'MPD cannot read ' + stream} : {};
  };
  await assert.rejects(Promise.resolve(controller.clearAddPlayTrack({uri: 'yam/track/123'})), /mpd.add/);
  assert.equal(calls.mpd.includes('play'), false);
  assert.equal(JSON.stringify(calls.toast).includes(stream), false);
});

test('account settings never return a token and logout stops only this source', async () => {
  const {controller, calls, router} = fixture();
  const commands = [];
  fakeBackend(controller, async (method, params) => {
    commands.push({method, params});
    return {configured: true, token: 'must-not-leak'};
  });
  const ui = await controller.saveAccount({token: 'test-token'});
  assert.equal(JSON.stringify(ui).includes('must-not-leak'), false);
  assert.equal(JSON.stringify(ui).includes('test-token'), false);
  assert.equal(commands[0].params.token, 'test-token');
  await controller.logoutAccount();
  assert.equal(calls.stopped, undefined);
  router.volumioGetState = () => ({service: 'yam'});
  await controller.logoutAccount();
  assert.equal(calls.stopped, true);
  calls.stopped = false;
  router.volumioGetState = () => ({service: 'mpd'});
  router.stateMachine.getTrack = () => ({service: 'yam'});
  await controller.logoutAccount();
  assert.equal(calls.stopped, true);
});

test('journal persists safe errors, rotates files and masks stored and submitted tokens', () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-journal-'));
  try {
    fs.writeFileSync(path.join(data, 'yandex-account.json'), JSON.stringify({token: 'stored-secret'}));
    const journal = new Journal(data);
    journal.protect('submitted-secret');
    journal.write('ERROR', 'play.mpd.add', 'stored-secret submitted-secret OAuth other-token https://cdn.yandex.net/file?s=secret password="hidden"');
    const contents = fs.readFileSync(journal.file, 'utf8');
    for (const secret of ['stored-secret', 'submitted-secret', 'other-token', 'https://', 'hidden']) {
      assert.equal(contents.includes(secret), false);
    }
    assert.match(new Journal(data).lastError(), /play.mpd.add/);
    assert.equal(fs.statSync(journal.file).mode & 0o777, 0o600);
    for (let i = 0; i < 300; i++) journal.write('INFO', 'test', 'Ж'.repeat(1000));
    assert.ok(fs.statSync(journal.file + '.1').size < 265000);
    assert.equal(journal.entries().length, 100);
    assert.match(journal.lastError(), /play.mpd.add/);
  } finally { fs.rmSync(data, {recursive: true, force: true}); }
});

test('play failure remains in diagnostics with its stage and journal HTML is escaped', async () => {
  const {controller, router} = fixture();
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-journal-'));
  try {
    controller.journal = new Journal(data);
    fakeBackend(controller, async () => ({uri: stream}));
    controller.mpd.sendMpdCommand = async command => {
      if (command.startsWith('add ')) throw new Error('MPD rejected ' + stream);
    };
    await assert.rejects(Promise.resolve(controller.clearAddPlayTrack({uri: 'yam/track/123'})));
    const ui = await controller.getUIConfig();
    const last = ui.sections.find(section => section.id === 'diagnostics').content[0].value;
    assert.match(last, /mpd.add/);
    assert.match(last, /MPD rejected/);
    assert.equal(last.includes(stream), false);
    controller.journal.write('ERROR', 'test', '<img src=x onerror=alert(1)>');
    let modal;
    router.broadcastMessage = (event, payload) => { assert.equal(event, 'openModal'); modal = payload; };
    await controller.showJournal();
    assert.equal(modal.message.includes('<img'), false);
    assert.ok(modal.message.includes('&lt;img'));
  } finally { fs.rmSync(data, {recursive: true, force: true}); }
});

test('repository update uses pinned package, native update API and survives its own onStop', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-updater-'));
  const journal = new Journal(data);
  let updater;
  const manager = {updatePlugin: async payload => {
    assert.equal(payload.name, 'yam');
    assert.equal(payload.category, 'music_service');
    const name = payload.url.split('/').pop();
    assert.equal(fs.readFileSync(path.join(data, 'drop', name), 'utf8'), 'pinned-package');
    updater.stop();
    assert.ok(fs.existsSync(updater.ready.file));
  }};
  updater = new Updater(data, manager, journal, {dropDirectory: path.join(data, 'drop')});
  const sha = 'a'.repeat(40);
  updater.run = async (action, output) => {
    fs.writeFileSync(output, action === 'prepare' ? 'pinned-package' : 'previous-package');
    return {version: '0.4.3', commit: sha};
  };
  try {
    await updater.check();
    await assert.rejects(updater.install('b'.repeat(40)), /Сначала/);
    await updater.install(sha);
    assert.equal(updater.busy, false);
    assert.equal(updater.ready, null);
    assert.equal(updater.work, null);
    assert.equal(fs.readFileSync(path.join(data, 'previous-plugin.zip'), 'utf8'), 'previous-package');
    assert.deepEqual(fs.readdirSync(path.join(data, 'drop')), []);
  } finally { updater.cleanup(); fs.rmSync(data, {recursive: true, force: true}); }
});

test('failed preparation and changed packages never call Volumio or stop playback', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-updater-'));
  let installs = 0;
  const updater = new Updater(data, {updatePlugin: () => { installs++; }}, new Journal(data));
  try {
    updater.run = async () => { throw new Error('GitHub HTTP 403'); };
    await assert.rejects(updater.check(), /403/);
    assert.equal(updater.ready, null);
    assert.equal(updater.busy, false);
    updater.run = async (action, output) => {
      fs.writeFileSync(output, 'valid');
      return {version: '0.4.3', commit: 'a'.repeat(40)};
    };
    await updater.check();
    fs.writeFileSync(updater.ready.file, 'tampered');
    await assert.rejects(updater.install('a'.repeat(40)), /изменился/);
    assert.equal(installs, 0);
  } finally { updater.cleanup(); fs.rmSync(data, {recursive: true, force: true}); }
});

test('duplicate check and install clicks are rejected while a job is running', async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-updater-'));
  const updater = new Updater(data, {}, new Journal(data));
  let complete;
  updater.run = () => new Promise(resolve => { complete = resolve; });
  try {
    const first = updater.check();
    await assert.rejects(updater.check(), /Дождитесь/);
    await assert.rejects(updater.install('a'.repeat(40)), /Дождитесь/);
    complete({current: true});
    await first;
    assert.equal(updater.busy, false);
    assert.equal(updater.ready, null);
  } finally { updater.cleanup(); fs.rmSync(data, {recursive: true, force: true}); }
});

test('playlist metadata serves 50 queue expansions without new API requests', async () => {
  const {controller} = fixture();
  let requests = 0;
  const tracks = Array.from({length: 50}, (_, i) => ({...track, id: String(i + 1)}));
  fakeBackend(controller, async method => {
    requests++;
    assert.equal(method, 'library');
    return {title: 'Playlist', tracks, next_offset: null};
  });
  const page = await controller.handleBrowseUri('yam/collection/likes');
  const items = await Promise.all(page.navigation.lists[0].items.map(item => controller.explodeUri(item)));
  assert.equal(items.length, 50);
  assert.equal(requests, 1);
  assert.equal(items[49][0].uri, 'yam/track/50');
});

test('worker queues a playlist burst and sends playback before waiting metadata', async () => {
  const Backend = require('../lib/backend');
  const backend = Object.create(Backend.prototype);
  Object.assign(backend, {pending: new Map(), queue: [], sequence: 0, buffer: '', closed: false});
  const sent = [];
  backend.process = {stdin: {write: line => sent.push(JSON.parse(line))}, exitCode: 0};
  const requests = Array.from({length: 50}, (_, i) => backend.request('track', {id: String(i)}));
  const playing = backend.request('stream', {id: '1'});
  try {
    assert.equal(sent.length, 1);
    backend.receive(JSON.stringify({id: sent[0].id, result: track}) + '\n');
    assert.equal(sent[1].method, 'stream');
    backend.receive(JSON.stringify({id: sent[1].id, result: {uri: stream}}) + '\n');
    assert.deepEqual(await playing, {uri: stream});
    for (let i = 2; i < 51; i++) {
      backend.receive(JSON.stringify({id: sent[i].id, result: track}) + '\n');
    }
    assert.equal((await Promise.all(requests)).length, 50);
    assert.equal(backend.pending.size, 0);
  } finally { await backend.stop(); }
});

test('stopping worker rejects both active and queued requests', async () => {
  const Backend = require('../lib/backend');
  const backend = Object.create(Backend.prototype);
  Object.assign(backend, {pending: new Map(), queue: [], sequence: 0, buffer: '', closed: false});
  backend.process = {stdin: {write: () => {}}, exitCode: 0};
  const results = Promise.allSettled([backend.request('track'), backend.request('stream')]);
  await backend.stop();
  assert.deepEqual((await results).map(result => result.status), ['rejected', 'rejected']);
  assert.equal(backend.queue.length, 0);
});
