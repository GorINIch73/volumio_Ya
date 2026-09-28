'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const execFile = require('child_process').execFile;

class Updater {
  constructor(directory, manager, journal, options) {
    this.directory = directory;
    this.manager = manager;
    this.journal = journal;
    this.root = options && options.root || path.join(__dirname, '..');
    this.dropDirectory = options && options.dropDirectory || '/tmp/plugins';
    this.busy = false;
    this.ready = null;
    this.message = 'Нажмите «Проверить обновление»';
    this.work = null;
    this.phase = null;
    this.worker = null;
  }

  run(action, output) {
    return new Promise((resolve, reject) => {
      const args = [path.join(this.root, 'python/plugin_update.py'), action, '--root', this.root];
      if (output) args.push('--output', output);
      this.worker = execFile('python3', args, {timeout: 120000, maxBuffer: 128 * 1024,
        env: Object.assign({}, process.env, {PYTHONDONTWRITEBYTECODE: '1'})}, (error, stdout) => {
        this.worker = null;
        let result;
        try { result = JSON.parse(stdout); } catch (_) {}
        if (result && result.error) return reject(new Error(result.error));
        if (error || !result) return reject(new Error('Не удалось подготовить обновление. Проверьте интернет и свободное место.'));
        resolve(result);
      });
    });
  }

  async check() {
    if (this.busy) throw new Error('Дождитесь завершения обновления');
    this.busy = true;
    this.phase = 'check';
    this.ready = null;
    this.message = 'Загрузка и проверка сборки из GitHub…';
    this.journal.write('INFO', 'update.check', this.message);
    try {
      this.cleanup();
      this.work = fs.mkdtempSync(path.join(os.tmpdir(), 'yam-update-'));
      const file = path.join(this.work, 'plugin.zip');
      const result = await this.run('prepare', file);
      if (result.current) {
        this.message = 'Установлена последняя сборка репозитория';
        this.cleanup();
      } else {
        this.ready = Object.assign({}, result, {file, digest: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')});
        this.message = 'Готова версия ' + result.version + ' · ' + result.commit.slice(0, 8) + '. Нажмите «Установить обновление».';
      }
      this.journal.write('INFO', 'update.check', this.message);
      return this.message;
    } catch (error) {
      this.message = this.journal.write('ERROR', 'update.check', error.message);
      this.cleanup();
      throw new Error(this.message);
    } finally { this.busy = false; this.phase = null; }
  }

  async install(commit) {
    if (this.busy) throw new Error('Дождитесь завершения обновления');
    if (!this.ready || commit !== this.ready.commit) throw new Error('Сначала проверьте обновление и выберите подготовленную сборку');
    if (typeof this.manager.updatePlugin !== 'function') throw new Error('Эта версия Volumio не предоставляет механизм обновления плагинов');
    this.busy = true;
    this.phase = 'install';
    const ready = this.ready;
    let dropped;
    try {
      const digest = crypto.createHash('sha256').update(fs.readFileSync(ready.file)).digest('hex');
      if (digest !== ready.digest) throw new Error('Подготовленный пакет изменился. Повторите проверку.');
      fs.mkdirSync(this.directory, {recursive: true, mode: 0o700});
      await this.run('backup', path.join(this.directory, 'previous-plugin.zip'));
      fs.mkdirSync(this.dropDirectory, {recursive: true});
      const name = 'yam-' + crypto.randomBytes(12).toString('hex') + '.zip';
      dropped = path.join(this.dropDirectory, name);
      fs.copyFileSync(ready.file, dropped, fs.constants.COPYFILE_EXCL);
      this.message = 'Установка через менеджер Volumio…';
      this.journal.write('INFO', 'update.install', 'Версия ' + ready.version + ' · ' + commit.slice(0, 8));
      await this.manager.updatePlugin({category: 'music_service', name: 'yam',
        url: 'http://127.0.0.1:3000/plugin-serve/' + name});
      this.message = 'Обновление установлено. Перезапустите Volumio для загрузки нового кода.';
      this.journal.write('INFO', 'update.install', this.message);
      this.ready = null;
      return this.message;
    } catch (error) {
      this.message = this.journal.write('ERROR', 'update.install',
        'Обновление не завершено: ' + error.message + '. Предыдущий пакет: previous-plugin.zip в каталоге настроек.');
      throw new Error(this.message);
    } finally {
      if (dropped) { try { fs.unlinkSync(dropped); } catch (_) {} }
      this.cleanup();
      this.ready = null;
      this.busy = false;
      this.phase = null;
    }
  }

  cleanup() {
    if (this.work) fs.rmSync(this.work, {recursive: true, force: true});
    this.work = null;
  }

  stop() {
    // Volumio invokes onStop from updatePlugin: do not cancel its own package handoff.
    if (!this.busy) { this.cleanup(); this.ready = null; }
    else if (this.phase === 'check' && this.worker) this.worker.kill('SIGTERM');
  }
}

module.exports = Updater;
