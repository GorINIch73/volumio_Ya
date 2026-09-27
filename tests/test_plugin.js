'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const Controller = require('../index');

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
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'media-str-plugin-'));
  router.pluginManager.getConfigurationFile = () => path.join(data, 'config.json');
  try {
    const starting = controller.onStart();
    assert.equal(typeof starting.fail, 'function');
    await Promise.all([starting, controller.onStart()]);
    assert.equal(calls.sources.length, 1);
    const worker = controller.backend.process;
    assert.equal((await controller.handleBrowseUri('media_str')).navigation.lists[0].items.length, 0);
    assert.equal((await controller.getUIConfig()).sections[0].content[0].value, '');
    await controller.onStop();
    assert.notEqual(worker.signalCode, null);
    await assert.rejects(Promise.resolve(controller.handleBrowseUri('media_str')), /Включите/);
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
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'media-str-plugin-'));
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
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'media-str-plugin-'));
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
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'media-str-plugin-'));
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
  const root = await controller.handleBrowseUri('media_str');
  assert.equal(root.navigation.prev.uri, '/');
  assert.equal(root.navigation.lists[0].items[1].uri, 'media_str/collection/42');
  const page = await controller.handleBrowseUri('media_str/collection/likes/50');
  const items = page.navigation.lists[0].items;
  assert.equal(items[0].type, 'song');
  assert.equal(items[1].type, 'item-no-menu');
  assert.equal(items[1].uri, undefined);
  assert.equal(items[2].uri, 'media_str/collection/likes/100');
  assert.equal(page.navigation.prev.uri, 'media_str/collection/likes/0');
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
  const queued = await controller.explodeUri({uri: 'media_str/track/123'});
  assert.deepEqual(methods, ['track']);
  assert.equal(queued[0].uri, 'media_str/track/123');
  assert.equal(queued[0].service, 'media_str');
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
  const playing = Promise.resolve(controller.clearAddPlayTrack({uri: 'media_str/track/123'}));
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
  const old = Promise.resolve(controller.clearAddPlayTrack({uri: 'media_str/track/123'}));
  await controller.clearAddPlayTrack({uri: 'media_str/track/124'});
  resolveOld({uri: stream});
  await assert.rejects(old);
  assert.equal(calls.mpd.filter(command => command === 'play').length, 1);
});

test('MPD failure does not disclose signed URLs', async () => {
  const {controller, calls} = fixture();
  fakeBackend(controller, async () => ({uri: stream}));
  controller.mpd.sendMpdCommand = async () => { throw new Error('Failed ' + stream); };
  await assert.rejects(Promise.resolve(controller.clearAddPlayTrack({uri: 'media_str/track/123'})), /Не удалось/);
  assert.equal(JSON.stringify(calls).includes(stream), false);
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
  router.volumioGetState = () => ({service: 'media_str'});
  await controller.logoutAccount();
  assert.equal(calls.stopped, true);
  calls.stopped = false;
  router.volumioGetState = () => ({service: 'mpd'});
  router.stateMachine.getTrack = () => ({service: 'media_str'});
  await controller.logoutAccount();
  assert.equal(calls.stopped, true);
});
