'use strict';

const fs = require('fs');
const path = require('path');
const VERSION = require('../package.json').version;
const LIMIT = 256 * 1024;

function redact(value, secrets) {
  let text = String(value || '');
  for (const secret of secrets || []) {
    if (typeof secret === 'string' && secret) text = text.split(secret).join('[скрыто]');
  }
  return text.replace(/https?:\/\/[^\s<>"']+/gi, '[URL скрыт]')
    .replace(/\b(?:OAuth|Bearer|Basic)\s+[^\s,;"']+/gi, '[авторизация скрыта]')
    .replace(/(["']?(?:token|access_token|refresh_token|password|cookie|authorization)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, '$1[скрыто]')
    .replace(/[A-Za-z0-9_~+/=-]{40,}/g, '[идентификатор скрыт]')
    .replace(/[\r\n\x00-\x1f]/g, ' ').slice(0, 1500);
}

class Journal {
  constructor(directory) {
    this.directory = directory;
    this.file = path.join(directory, 'plugin-journal.jsonl');
    this.secrets = new Set();
    this.memory = [];
  }

  protect(token) { if (typeof token === 'string' && token) this.secrets.add(token); }

  sanitize(message) {
    // Include the persisted token after restarts, but never log the account file.
    try { this.protect(JSON.parse(fs.readFileSync(path.join(this.directory, 'yandex-account.json'), 'utf8')).token); } catch (_) {}
    return redact(message, this.secrets);
  }

  write(level, operation, message) {
    const entry = {time: new Date().toISOString(), version: VERSION, level,
      operation: this.sanitize(operation), message: this.sanitize(message)};
    try {
      fs.mkdirSync(this.directory, {recursive: true, mode: 0o700});
      if (fs.existsSync(this.file) && fs.statSync(this.file).size >= LIMIT) {
        fs.renameSync(this.file, this.file + '.1');
      }
      fs.appendFileSync(this.file, JSON.stringify(entry) + '\n', {mode: 0o600});
      if (level === 'ERROR') {
        const temporary = path.join(this.directory, 'last-error.tmp');
        fs.writeFileSync(temporary, JSON.stringify(entry), {mode: 0o600});
        fs.renameSync(temporary, path.join(this.directory, 'last-error.json'));
      }
    } catch (_) {
      this.memory.push(entry);
      this.memory = this.memory.slice(-100);
    }
    return entry.message;
  }

  entries() {
    const result = [];
    for (const name of [this.file + '.1', this.file]) {
      try {
        for (const line of fs.readFileSync(name, 'utf8').split('\n')) {
          try { result.push(JSON.parse(line)); } catch (_) {}
        }
      } catch (_) {}
    }
    return result.concat(this.memory).slice(-100);
  }

  lastError() {
    try {
      const last = JSON.parse(fs.readFileSync(path.join(this.directory, 'last-error.json'), 'utf8'));
      return this.sanitize(last.time + ' ' + last.operation + ': ' + last.message);
    } catch (_) {}
    const entries = this.entries().filter(entry => entry.level === 'ERROR');
    const last = entries[entries.length - 1];
    return last ? this.sanitize(last.time + ' ' + last.operation + ': ' + last.message) : 'Ошибок пока нет';
  }

  text() {
    return this.entries().map(entry => this.sanitize(
      entry.time + ' [' + entry.version + '] ' + entry.level + ' ' + entry.operation + ': ' + entry.message
    )).join('\n') || 'Журнал пока пуст';
  }
}

module.exports = {Journal, redact};
