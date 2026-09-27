'use strict';

const fs = require('fs');
const path = require('path');
const libQ = require('kew');
const Backend = require('./lib/backend');
const SERVICE = 'media_str';
const NAME = 'Яндекс Музыка · Media Str';

function ControllerMediaStr(context) {
  this.context = context;
  this.commandRouter = context.coreCommand;
  this.logger = context.logger;
  this.backend = null;
  this.started = false;
  this.lifecycle = 0;
  this.generation = 0;
  this.playback = Promise.resolve();
}

ControllerMediaStr.prototype.getConfigurationFiles = function () { return ['config.json']; };

ControllerMediaStr.prototype.onVolumioStart = async function () {
  const config = this.commandRouter.pluginManager.getConfigurationFile(this.context, 'config.json');
  this.data = path.dirname(config);
};

ControllerMediaStr.prototype.onStart = async function () {
  if (this.started) return;
  if (this.starting) return this.starting;
  const epoch = ++this.lifecycle;
  this.starting = this.startBackend(epoch);
  try { await this.starting; } finally { this.starting = null; }
};

ControllerMediaStr.prototype.startBackend = async function (epoch) {
  if (!this.data) await this.onVolumioStart();
  if (epoch !== this.lifecycle) throw new Error('Запуск плагина отменён');
  this.mpd = this.commandRouter.pluginManager.getPlugin('music_service', 'mpd');
  if (!this.mpd) throw new Error('Проигрыватель MPD недоступен');
  const backend = new Backend(this.data, () => {
    if (this.backend !== backend) return;
    this.started = false;
    this.generation++;
    this.removeFromBrowseSources();
    this.logger.error('Media Str: компонент Яндекс Музыки остановлен');
  });
  this.backend = backend;
  try {
    await backend.request('ping');
    if (epoch !== this.lifecycle) throw new Error('Запуск плагина отменён');
    this.started = true;
    this.addToBrowseSources();
  } catch (error) {
    this.started = false;
    await backend.stop();
    if (this.backend === backend) this.backend = null;
    throw error;
  }
};

ControllerMediaStr.prototype.addToBrowseSources = function () {
  this.commandRouter.volumioAddToBrowseSources({
    name: NAME, uri: SERVICE, plugin_type: 'music_service', plugin_name: SERVICE, icon: 'fa fa-music'
  });
};

ControllerMediaStr.prototype.removeFromBrowseSources = function () {
  this.commandRouter.volumioRemoveToBrowseSources(NAME);
};

ControllerMediaStr.prototype.onStop = async function () {
  this.lifecycle++;
  this.started = false;
  this.generation++;
  this.removeFromBrowseSources();
  const backend = this.backend;
  this.backend = null;
  if (backend) await backend.stop();
  if (this.starting) await Promise.resolve(this.starting).catch(() => {});
  await this.playback.catch(() => {});
  if (this.ownsPlayback()) await this.commandRouter.volumioStop();
};

ControllerMediaStr.prototype.ownsPlayback = function () {
  const state = this.commandRouter.volumioGetState();
  if (!state) return false;
  if (state.service === SERVICE) return true;
  // Before MPD's first metadata update consume mode can still report 'mpd'.
  const machine = this.commandRouter.stateMachine;
  const track = state.service === 'mpd' && typeof machine.getTrack === 'function'
    ? machine.getTrack(machine.currentPosition) : null;
  return Boolean(track && track.service === SERVICE);
};

ControllerMediaStr.prototype.onRestart = async function () {
  await this.onStop();
  await this.onStart();
};

ControllerMediaStr.prototype.request = function (method, params) {
  if (!this.started || !this.backend) return Promise.reject(new Error('Включите плагин Яндекс Музыки'));
  return this.backend.request(method, params);
};

function navigation(title, items, previous) {
  return {navigation: {prev: {uri: previous || SERVICE}, lists: [{
    title, availableListViews: ['list'], items
  }]}};
}

function folder(title, uri, icon) {
  // Collections are browse-only until whole-playlist enqueue is implemented.
  return {service: SERVICE, type: 'item-no-menu', title, uri, icon: icon || 'fa fa-folder-open-o'};
}

function trackItem(track, type) {
  return {service: SERVICE, type: type || 'song', uri: SERVICE + '/track/' + track.id,
    title: track.title, name: track.title, artist: track.artist, album: track.album,
    duration: track.duration, trackType: 'mp3', icon: 'fa fa-music'};
}

function identifier(data) {
  const uri = typeof data === 'string' ? data : data && data.uri;
  const match = typeof uri === 'string' && /^media_str\/track\/([0-9]{1,24}(?::[0-9]{1,24})?)$/.exec(uri);
  if (!match) throw new Error('Некорректный адрес трека');
  return match[1];
}

ControllerMediaStr.prototype.handleBrowseUri = async function (uri) {
  if (uri === SERVICE) {
    const status = await this.request('status');
    if (!status.configured) {
      return navigation('Войдите в аккаунт в настройках плагина', [], '/');
    }
    const result = await this.request('library');
    return navigation(NAME, [folder('Мне нравится', SERVICE + '/collection/likes', 'fa fa-heart')]
      .concat(result.playlists.map(item => folder(item.title, SERVICE + '/collection/' + item.kind))), '/');
  }
  const match = /^media_str\/collection\/(likes|[0-9]{1,24})(?:\/([0-9]{1,6}))?$/.exec(uri);
  if (!match) throw new Error('Неизвестный раздел Яндекс Музыки');
  const offset = Number(match[2] || 0);
  const result = await this.request('library', {kind: match[1], offset});
  const items = result.tracks.map(track => track.available ? trackItem(track) :
    {service: SERVICE, type: 'item-no-menu', title: track.title + ' — недоступен', icon: 'fa fa-ban'});
  if (result.next_offset !== null) {
    items.push(folder('Следующие 50 треков', SERVICE + '/collection/' + match[1] + '/' + result.next_offset));
  }
  const previous = offset ? SERVICE + '/collection/' + match[1] + '/' + Math.max(0, offset - 50) : SERVICE;
  return navigation(result.title, items, previous);
};

ControllerMediaStr.prototype.explodeUri = async function (data) {
  const track = await this.request('track', {id: identifier(data)});
  return [trackItem(track, 'track')];
};

ControllerMediaStr.prototype.getTrackInfo = async function (data) {
  const track = await this.request('track', {id: identifier(data)});
  return [trackItem(track)];
};

ControllerMediaStr.prototype.search = async function () {
  return []; // Global search must not fail while this source has no search support.
};

ControllerMediaStr.prototype.clearAddPlayTrack = async function (track) {
  const generation = ++this.generation;
  const current = () => {
    if (!this.started || generation !== this.generation) throw new Error('Запуск трека отменён');
  };
  try {
    const stream = await this.request('stream', {id: identifier(track)});
    current();
    // Signed URLs never enter Volumio's persisted queue or plugin logs.
    if (!/^https:\/\/[a-zA-Z0-9.-]+\/get-mp3\/[a-zA-Z0-9/_.%-]+$/.test(stream.uri)) {
      throw new Error('Некорректный адрес аудиофайла');
    }
    const previous = this.playback;
    this.playback = (async () => {
      await previous.catch(() => {});
      current();
      await this.mpd.sendMpdCommand('stop', []);
      current();
      await this.mpd.sendMpdCommand('clear', []);
      current();
      await this.mpd.sendMpdCommand('add "' + stream.uri + '"', []);
      current();
      await this.commandRouter.stateMachine.setConsumeUpdateService('mpd', true);
      current();
      await this.mpd.sendMpdCommand('play', []);
    })();
    await this.playback;
  } catch (error) {
    // MPD errors may echo signed URLs; never forward their raw messages.
    const safe = new Error('Не удалось запустить трек. Проверьте аккаунт и подключение.');
    if (generation === this.generation) this.commandRouter.pushToastMessage('error', NAME, safe.message);
    throw safe;
  }
};

ControllerMediaStr.prototype.stop = async function () {
  this.generation++;
  await this.playback.catch(() => {});
  if (this.mpd) await this.mpd.sendMpdCommand('stop', []);
};
ControllerMediaStr.prototype.pause = async function () { await this.mpd.sendMpdCommand('pause 1', []); };
ControllerMediaStr.prototype.resume = async function () {
  await this.commandRouter.stateMachine.setConsumeUpdateService('mpd', true);
  await this.mpd.sendMpdCommand('pause 0', []);
};
ControllerMediaStr.prototype.seek = async function (position) { return this.mpd.seek(position); };
ControllerMediaStr.prototype.next = async function () {
  // Consume updates come from MPD, but track selection belongs to Volumio's queue.
  await this.commandRouter.stateMachine.setConsumeUpdateService(undefined);
  return this.commandRouter.stateMachine.next();
};
ControllerMediaStr.prototype.previous = async function () {
  await this.commandRouter.stateMachine.setConsumeUpdateService(undefined);
  return this.commandRouter.stateMachine.previous();
};

ControllerMediaStr.prototype.getUIConfig = async function () {
  const ui = JSON.parse(fs.readFileSync(path.join(__dirname, 'UIConfig.json'), 'utf8'));
  if (this.started) {
    try {
      const status = await this.request('status');
      ui.sections[0].label = status.configured ? 'Аккаунт подключён' : 'Вход в Яндекс Музыку';
    } catch (_) {
      ui.sections[0].label = 'Не удалось прочитать аккаунт. Войдите заново.';
    }
  } else ui.sections[0].label = 'Сначала включите плагин';
  return ui;
};

ControllerMediaStr.prototype.saveAccount = async function (data) {
  await this.request('login', {token: data && data.token});
  this.commandRouter.pushToastMessage('success', NAME, 'Аккаунт подключён. Откройте «Обзор».');
  return this.getUIConfig();
};
ControllerMediaStr.prototype.checkAccount = async function () {
  await this.request('check');
  this.commandRouter.pushToastMessage('success', NAME, 'Аккаунт доступен');
};
ControllerMediaStr.prototype.logoutAccount = async function () {
  if (this.ownsPlayback()) await this.commandRouter.volumioStop();
  await this.request('logout');
  this.commandRouter.pushToastMessage('success', NAME, 'Вы вышли из аккаунта');
  return this.getUIConfig();
};

// Volumio calls .fail()/.fin() on plugin promises; expose its native Kew contract.
Object.keys(ControllerMediaStr.prototype).forEach(name => {
  const implementation = ControllerMediaStr.prototype[name];
  if (implementation.constructor.name !== 'AsyncFunction') return;
  ControllerMediaStr.prototype[name] = function () {
    const deferred = libQ.defer();
    implementation.apply(this, arguments).then(value => deferred.resolve(value), error => deferred.reject(error));
    return deferred.promise;
  };
});

module.exports = ControllerMediaStr;
