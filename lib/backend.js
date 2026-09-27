'use strict';

const spawn = require('child_process').spawn;
const path = require('path');

class Backend {
  constructor(data, onFailure) {
    this.pending = new Map();
    this.sequence = 0;
    this.buffer = '';
    this.closed = false;
    this.onFailure = onFailure;
    this.process = spawn('python3', ['-u', path.join(__dirname, '../python/bridge.py'), '--data', data], {
      stdio: ['pipe', 'pipe', 'ignore'],
      env: Object.assign({}, process.env, {PYTHONDONTWRITEBYTECODE: '1'})
    });
    this.process.stdout.setEncoding('utf8');
    this.process.stdout.on('data', chunk => this.receive(chunk));
    this.process.stdin.on('error', () => this.fail());
    this.process.on('error', () => this.fail());
    this.process.on('exit', () => this.fail());
  }

  request(method, params) {
    if (this.closed) return Promise.reject(new Error('Плагин остановлен. Включите его заново.'));
    if (this.pending.size >= 16) return Promise.reject(new Error('Дождитесь завершения запросов'));
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(), 45000);
      this.pending.set(id, {resolve, reject, timer});
      this.process.stdin.write(JSON.stringify({id, method, params: params || {}}) + '\n');
    });
  }

  receive(chunk) {
    this.buffer += chunk;
    if (this.buffer.length > 8 * 1024 * 1024) return this.fail();
    let end;
    while ((end = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(line); } catch (_) { return this.fail(); }
      const pending = this.pending.get(message.id);
      if (!pending) continue;
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error));
      else pending.resolve(message.result);
    }
  }

  fail() {
    if (this.closed) return;
    this.stop();
    if (this.onFailure) this.onFailure();
  }

  stop() {
    if (this.closed) return this.stopped || Promise.resolve();
    this.closed = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error('Компонент Яндекс Музыки остановлен. Включите плагин заново.'));
    }
    this.pending.clear();
    this.stopped = new Promise(resolve => {
      if (this.process.exitCode !== null || this.process.signalCode !== null || !this.process.pid) return resolve();
      const timer = setTimeout(() => this.process.kill('SIGKILL'), 2000);
      this.process.once('exit', () => { clearTimeout(timer); resolve(); });
      this.process.kill('SIGTERM');
    });
    return this.stopped;
  }
}

module.exports = Backend;
