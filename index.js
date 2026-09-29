'use strict';

const fs = require('fs');
const path = require('path');
const libQ = require('kew');
const Backend = require('./lib/backend');
const {Journal, redact} = require('./lib/journal');
const Updater = require('./lib/updater');
const SERVICE = 'yam';
const NAME = 'YaM';

function ControllerYaM(context) {
  this.context = context;
  this.commandRouter = context.coreCommand;
  this.logger = context.logger;
  this.backend = null;
  this.started = false;
  this.lifecycle = 0;
  this.generation = 0;
  this.playback = Promise.resolve();
  this.browsePlayback = Promise.resolve();
  this.trackCache = new Map();
  this.journal = null;
  this.updater = null;
}

ControllerYaM.prototype.getConfigurationFiles = function () { return ['config.json']; };

ControllerYaM.prototype.onVolumioStart = async function () {
  const config = this.commandRouter.pluginManager.getConfigurationFile(this.context, 'config.json');
  this.data = path.dirname(config);
  if (!this.journal) this.journal = new Journal(this.data);
  if (!this.updater) this.updater = new Updater(this.data, this.commandRouter.pluginManager, this.journal);
};

ControllerYaM.prototype.note = function (level, operation, message) {
  return this.journal ? this.journal.write(level, operation, message) : redact(message);
};

ControllerYaM.prototype.onStart = async function () {
  if (this.started) return;
  if (this.starting) return this.starting;
  const epoch = ++this.lifecycle;
  this.starting = this.startBackend(epoch);
  try { await this.starting; } finally { this.starting = null; }
};

ControllerYaM.prototype.startBackend = async function (epoch) {
  if (!this.data) await this.onVolumioStart();
  if (epoch !== this.lifecycle) throw new Error('Запуск плагина отменён');
  this.mpd = this.commandRouter.pluginManager.getPlugin('music_service', 'mpd');
  if (!this.mpd) throw new Error('Проигрыватель MPD недоступен');
  const backend = new Backend(this.data, reason => {
    if (this.backend !== backend) return;
    this.started = false;
    this.generation++;
    this.removeFromBrowseSources();
    this.note('ERROR', 'backend', reason || 'Компонент Яндекс Музыки остановлен');
    this.logger.error('YaM: компонент Яндекс Музыки остановлен');
  });
  this.backend = backend;
  try {
    await backend.request('ping');
    if (epoch !== this.lifecycle) throw new Error('Запуск плагина отменён');
    this.started = true;
    this.addToBrowseSources();
    this.note('INFO', 'start', 'Плагин запущен');
  } catch (error) {
    this.started = false;
    await backend.stop();
    if (this.backend === backend) this.backend = null;
    throw error;
  }
};

ControllerYaM.prototype.addToBrowseSources = function () {
  this.commandRouter.volumioAddToBrowseSources({
    name: NAME, uri: SERVICE, plugin_type: 'music_service', plugin_name: SERVICE, icon: 'fa fa-music'
  });
};

ControllerYaM.prototype.removeFromBrowseSources = function () {
  this.commandRouter.volumioRemoveToBrowseSources(NAME);
};

ControllerYaM.prototype.onStop = async function () {
  if (this.updater) this.updater.stop();
  this.trackCache.clear();
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
  this.note('INFO', 'stop', 'Плагин остановлен');
};

ControllerYaM.prototype.ownsPlayback = function () {
  const state = this.commandRouter.volumioGetState();
  if (!state) return false;
  if (state.service === SERVICE) return true;
  // Before MPD's first metadata update consume mode can still report 'mpd'.
  const machine = this.commandRouter.stateMachine;
  const track = state.service === 'mpd' && typeof machine.getTrack === 'function'
    ? machine.getTrack(machine.currentPosition) : null;
  return Boolean(track && track.service === SERVICE);
};

ControllerYaM.prototype.onRestart = async function () {
  await this.onStop();
  await this.onStart();
};

ControllerYaM.prototype.request = function (method, params) {
  if (!this.started || !this.backend) return Promise.reject(new Error('Включите плагин Яндекс Музыки'));
  return this.backend.request(method, params).catch(error => {
    error.message = this.note('ERROR', 'yandex.' + method,
      error.message + (error.diagnostic ? ' (' + error.diagnostic + ')' : ''));
    error.logged = true;
    throw error;
  });
};

function navigation(title, items, previous, views) {
  return {navigation: {prev: {uri: previous || SERVICE}, lists: [{
    title, availableListViews: views || ['list'], items
  }]}};
}

function folder(title, uri, icon, albumart) {
  // Collections are browse-only until whole-playlist enqueue is implemented.
  return {service: SERVICE, type: 'item-no-menu', title, uri,
    albumart: albumart || '/albumart', icon: icon || 'fa fa-folder-open-o'};
}

function trackItem(track, type) {
  return {service: SERVICE, type: type || 'song', uri: SERVICE + '/track/' + track.id,
    title: track.title, name: track.title, artist: track.artist, album: track.album,
    duration: track.duration, albumart: track.albumart || '/albumart',
    trackType: 'mp3', icon: 'fa fa-music'};
}

function identifier(data) {
  const uri = typeof data === 'string' ? data : data && data.uri;
  const match = typeof uri === 'string' && /^yam\/track\/([0-9]{1,24}(?::[0-9]{1,24})?)$/.exec(uri);
  if (!match) throw new Error('Некорректный адрес трека');
  return match[1];
}

ControllerYaM.prototype.handleBrowseUri = async function (uri) {
  if (uri === SERVICE) {
    const status = await this.request('status');
    if (!status.configured) {
      return navigation('Войдите в аккаунт в настройках плагина', [], '/');
    }
    const result = await this.request('library');
    const page = navigation('Моя музыка', [folder('Мне нравится', SERVICE + '/collection/likes', 'fa fa-heart')]
      .concat(result.playlists.map(item => Object.assign(
        folder(item.title, SERVICE + '/collection/' + item.kind, undefined, item.albumart),
        {meta: 'Открыть список треков'}))), '/', ['grid', 'list']);
    try {
      const catalog = await this.request('catalog');
      for (const section of catalog.sections) {
        page.navigation.lists.push({title: section.title, availableListViews: ['grid', 'list'],
          items: section.items.map(item => folder(item.title, item.uri, undefined, item.albumart))});
      }
    } catch (_) {
      page.navigation.lists.push({title: 'Подборки временно недоступны. Откройте YaM ещё раз, чтобы повторить.',
        availableListViews: ['list'], items: []});
    }
    return page;
  }
  const collection = /^yam\/collection\/(likes|[0-9]{1,24})(?:\/([0-9]{1,6}))?$/.exec(uri);
  const playlist = /^yam\/playlist\/([0-9]{1,24})\/([0-9]{1,24})(?:\/([0-9]{1,6}))?$/.exec(uri);
  const album = /^yam\/album\/([0-9]{1,24})(?:\/([0-9]{1,6}))?$/.exec(uri);
  let params, base, offset;
  if (collection) {
    offset = Number(collection[2] || 0);
    params = {kind: collection[1], offset};
    base = SERVICE + '/collection/' + collection[1];
  } else if (playlist) {
    offset = Number(playlist[3] || 0);
    params = {owner: playlist[1], kind: playlist[2], offset};
    base = SERVICE + '/playlist/' + playlist[1] + '/' + playlist[2];
  } else if (album) {
    offset = Number(album[2] || 0);
    params = {album: album[1], offset};
    base = SERVICE + '/album/' + album[1];
  } else throw new Error('Неизвестный раздел Яндекс Музыки');
  const result = await this.request('library', params);
  for (const track of result.tracks) this.cacheTrack(track);
  const items = result.tracks.map(track => track.available ? trackItem(track) :
    {service: SERVICE, type: 'item-no-menu', title: track.title + ' — недоступен', icon: 'fa fa-ban'});
  if (result.next_offset !== null) {
    items.push(folder('Следующие 50 треков', base + '/' + result.next_offset));
  }
  const previous = offset ? base + '/' + Math.max(0, offset - 50) : SERVICE;
  return navigation(result.title, items, previous);
};

ControllerYaM.prototype.cacheTrack = function (track) {
  this.trackCache.delete(String(track.id));
  this.trackCache.set(String(track.id), {track, expires: Date.now() + 5 * 60 * 1000});
  if (this.trackCache.size > 500) this.trackCache.delete(this.trackCache.keys().next().value);
};

ControllerYaM.prototype.resolveTrack = async function (data) {
  const id = identifier(data);
  const cached = this.trackCache.get(id);
  if (cached && cached.expires > Date.now() && cached.track.available !== false) return cached.track;
  const track = await this.request('track', {id});
  this.cacheTrack(track);
  return track;
};

ControllerYaM.prototype.explodeUri = async function (data) {
  this.note('INFO', 'queue', 'Получение данных трека');
  const track = await this.resolveTrack(data);
  return [trackItem(track, 'track')];
};

ControllerYaM.prototype.getTrackInfo = async function (data) {
  const track = await this.resolveTrack(data);
  return [trackItem(track)];
};

ControllerYaM.prototype.search = async function () {
  return []; // Global search must not fail while this source has no search support.
};

ControllerYaM.prototype.mpdCommand = async function (command) {
  const result = await this.mpd.sendMpdCommand(command, []);
  // Volumio can resolve an MPD error as an object after showing a short toast.
  if (result && result.error) throw new Error(this.journal ? this.journal.sanitize(result.error) : redact(result.error));
  return result;
};

// A normal Volumio browse click replaces the queue. YaM keeps earlier pages
// so that Previous can reach tracks played before the latest selection.
ControllerYaM.prototype.playFromBrowse = async function (data) {
  const selected = identifier(data && data.item);
  const source = Array.isArray(data.list) ? data.list : [data.item];
  if (source.length > 100) throw new Error('Слишком много треков');
  let selectedIndex = -1;
  const items = [];
  const explicitIndex = Number.isInteger(data.index) && data.index >= 0 && data.index < source.length;
  source.forEach((item, index) => {
    if (!item || item.service !== SERVICE || item.type !== 'song') return;
    const id = identifier(item);
    if (id === selected && (explicitIndex ? index === data.index : selectedIndex === -1)) {
      selectedIndex = items.length;
    }
    items.push({service: SERVICE, uri: SERVICE + '/track/' + id});
  });
  if (selectedIndex < 0) throw new Error('Выбранный трек отсутствует в списке');
  const epoch = this.lifecycle;
  const previous = this.browsePlayback;
  this.browsePlayback = (async () => {
    await previous.catch(() => {});
    if (!this.started || epoch !== this.lifecycle) throw new Error('Запуск трека отменён');
    this.commandRouter.preLoadItemsStop();
    const result = await this.commandRouter.addQueueItems(items);
    if (!this.started || epoch !== this.lifecycle) throw new Error('Запуск трека отменён');
    return this.commandRouter.volumioPlay(result.firstItemIndex + selectedIndex);
  })();
  return this.browsePlayback;
};

ControllerYaM.prototype.clearAddPlayTrack = async function (track) {
  const generation = ++this.generation;
  let stage = 'stream';
  const current = () => {
    if (!this.started || generation !== this.generation) throw new Error('Запуск трека отменён');
  };
  try {
    this.note('INFO', 'play.stream', 'Получение ссылки на аудио');
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
      stage = 'mpd.stop';
      await this.mpdCommand('stop');
      current();
      stage = 'mpd.clear';
      await this.mpdCommand('clear');
      current();
      stage = 'mpd.add';
      await this.mpdCommand('add "' + stream.uri + '"');
      current();
      stage = 'state';
      await this.commandRouter.stateMachine.setConsumeUpdateService('mpd', true);
      current();
      stage = 'mpd.play';
      await this.mpdCommand('play');
    })();
    await this.playback;
    this.note('INFO', 'play', 'Трек передан проигрывателю');
    if (generation === this.generation) {
      try {
        this.commandRouter.broadcastMessage('yamPlaybackStarted', {uri: track.uri || track});
      } catch (_) {
        this.note('WARN', 'play.ui', 'Не удалось открыть экран текущего трека');
      }
    }
  } catch (error) {
    // MPD errors may echo signed URLs; never forward their raw messages.
    const message = 'Не удалось запустить трек (' + stage + '): ' + error.message;
    const safe = new Error(generation === this.generation
      ? this.note('ERROR', 'play.' + stage, message) : 'Запуск трека отменён');
    safe.logged = true;
    if (generation === this.generation) this.commandRouter.pushToastMessage('error', NAME,
      safe.message.slice(0, 180) + '. Подробности в журнале плагина.');
    throw safe;
  }
};

ControllerYaM.prototype.stop = async function () {
  this.generation++;
  await this.playback.catch(() => {});
  if (this.mpd) await this.mpdCommand('stop');
};
ControllerYaM.prototype.pause = async function () { await this.mpdCommand('pause 1'); };
ControllerYaM.prototype.resume = async function () {
  await this.commandRouter.stateMachine.setConsumeUpdateService('mpd', true);
  await this.mpdCommand('pause 0');
};
ControllerYaM.prototype.seek = async function (position) { return this.mpd.seek(position); };
ControllerYaM.prototype.next = async function () {
  // Consume updates come from MPD, but track selection belongs to Volumio's queue.
  await this.commandRouter.stateMachine.setConsumeUpdateService(undefined);
  return this.commandRouter.stateMachine.next();
};
ControllerYaM.prototype.previous = async function () {
  await this.commandRouter.stateMachine.setConsumeUpdateService(undefined);
  return this.commandRouter.stateMachine.previous();
};

ControllerYaM.prototype.getUIConfig = async function () {
  if (!this.data && typeof this.commandRouter.pluginManager.getConfigurationFile === 'function') await this.onVolumioStart();
  const ui = JSON.parse(fs.readFileSync(path.join(__dirname, 'UIConfig.json'), 'utf8'));
  if (this.started) {
    try {
      const status = await this.request('status');
      ui.sections[0].label = status.configured ? 'Аккаунт подключён' : 'Вход в Яндекс Музыку';
    } catch (_) {
      ui.sections[0].label = 'Не удалось прочитать аккаунт. Войдите заново.';
    }
  } else ui.sections[0].label = 'Сначала включите плагин';
  const diagnostics = ui.sections.find(section => section.id === 'diagnostics');
  diagnostics.content.find(item => item.id === 'last_error').value = this.journal ? this.journal.lastError() : 'Ошибок пока нет';
  const updates = ui.sections.find(section => section.id === 'updates');
  updates.content.find(item => item.id === 'update_status').value = this.updater ? this.updater.message : 'Включите плагин';
  updates.content.find(item => item.id === 'installed_version').value = require('./package.json').version;
  const install = updates.content.find(item => item.id === 'install_update');
  install.onClick.data = {commit: this.updater && this.updater.ready ? this.updater.ready.commit : ''};
  return ui;
};

ControllerYaM.prototype.refreshDiagnostics = async function () {
  const ui = await this.getUIConfig();
  this.commandRouter.broadcastMessage('pushUiConfig', ui);
  return ui;
};

ControllerYaM.prototype.showJournal = async function () {
  if (!this.journal) await this.onVolumioStart();
  const text = this.journal.text().replace(/[&<>"']/g, character =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[character]);
  this.commandRouter.broadcastMessage('openModal', {
    title: 'Журнал YaM · последние 100 записей', size: 'lg',
    message: '<p>Текст можно выделить и скопировать. Последняя ошибка также доступна отдельным полем в настройках.</p><pre>' + text + '</pre>',
    buttons: [{name: 'Закрыть', class: 'btn btn-info'}]
  });
};

ControllerYaM.prototype.checkRepository = async function () {
  if (!this.updater) await this.onVolumioStart();
  this.commandRouter.pushToastMessage('info', NAME, 'Проверяем репозиторий и готовим пакет…');
  try { await this.updater.check(); } finally { await this.refreshDiagnostics(); }
};

ControllerYaM.prototype.installRepository = async function (data) {
  if (!this.updater) throw new Error('Сначала проверьте обновление');
  try {
    await this.updater.install(data && data.commit);
    this.commandRouter.pushToastMessage('success', NAME, 'Обновление установлено. Volumio перезапускается…');
    // Reload all plugin modules through Volumio's normal service startup.
    setTimeout(() => {
      require('child_process').execFile('sudo', ['-n', 'systemctl', 'restart', 'volumio'], {timeout: 15000}, error => {
        if (error) {
          this.note('ERROR', 'update.restart', 'Не удалось перезапустить Volumio автоматически. Перезапустите устройство из меню Volumio.');
          this.commandRouter.pushToastMessage('error', NAME, 'Обновление установлено. Перезапустите устройство из меню Volumio.');
        }
      });
    }, 1500);
  } finally { await this.refreshDiagnostics(); }
};

ControllerYaM.prototype.saveAccount = async function (data) {
  if (this.journal) this.journal.protect(data && data.token);
  this.trackCache.clear();
  await this.request('login', {token: data && data.token});
  this.commandRouter.pushToastMessage('success', NAME, 'Аккаунт подключён. Откройте «Обзор».');
  return this.getUIConfig();
};
ControllerYaM.prototype.checkAccount = async function () {
  await this.request('check');
  this.commandRouter.pushToastMessage('success', NAME, 'Аккаунт доступен');
};
ControllerYaM.prototype.logoutAccount = async function () {
  if (this.ownsPlayback()) await this.commandRouter.volumioStop();
  this.trackCache.clear();
  await this.request('logout');
  this.commandRouter.pushToastMessage('success', NAME, 'Вы вышли из аккаунта');
  return this.getUIConfig();
};

// Volumio calls .fail()/.fin() on plugin promises; expose its native Kew contract.
Object.keys(ControllerYaM.prototype).forEach(name => {
  const implementation = ControllerYaM.prototype[name];
  if (implementation.constructor.name !== 'AsyncFunction') return;
  ControllerYaM.prototype[name] = function () {
    const deferred = libQ.defer();
    implementation.apply(this, arguments).then(value => deferred.resolve(value), error => {
      const safe = new Error(error.logged ? error.message : this.note('ERROR', name, error.message));
      safe.logged = true;
      deferred.reject(safe);
    });
    return deferred.promise;
  };
});

module.exports = ControllerYaM;
