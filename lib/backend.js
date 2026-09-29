'use strict';

const spawn = require('child_process').spawn;
const path = require('path');

class Backend {
  constructor(data, onFailure) {
    this.pending = new Map();
    this.sequence = 0;
    this.queue = [];
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
    this.process.on('error', error => this.fail('Не удалось запустить Python: ' + error.code));
    this.process.on('exit', (code, signal) => this.fail('Python завершился: code=' + code + ', signal=' + signal));
  }

  request(method, params) {
    if (this.closed) return Promise.reject(new Error('Плагин остановлен. Включите его заново.'));
    if (this.queue.length >= (method === 'stream' ? 528 : 512)) {
      return Promise.reject(new Error('Слишком много запросов в очереди'));
    }
    return new Promise((resolve, reject) => {
      const request = {id: ++this.sequence, method, params: params || {}, resolve, reject};
      // Python is sequential. Keep waiting work here so playback can go next,
      // and start the timeout only when the worker actually receives a request.
      if (method === 'stream') this.queue.unshift(request);
      else this.queue.push(request);
      this.drain();
    });
  }

  drain() {
    if (this.closed || this.pending.size || !this.queue.length) return;
    const request = this.queue.shift();
    request.timer = setTimeout(() => this.fail('Превышено время запроса: ' + request.method), 45000);
    this.pending.set(request.id, request);
    this.process.stdin.write(JSON.stringify({id: request.id, method: request.method, params: request.params}) + '\n');
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
      if (message.error) {
        const error = new Error(message.error);
        error.diagnostic = message.diagnostic;
        pending.reject(error);
      }
      else pending.resolve(message.result);
      this.drain();
    }
  }

  fail(reason) {
    if (this.closed) return;
    this.stop();
    if (this.onFailure) this.onFailure(reason || 'Ошибка обмена с Python');
  }

  stop() {
    if (this.closed) return this.stopped || Promise.resolve();
    this.closed = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error('Компонент Яндекс Музыки остановлен. Включите плагин заново.'));
    }
    this.pending.clear();
    for (const request of this.queue) {
      request.reject(new Error('Компонент Яндекс Музыки остановлен. Включите плагин заново.'));
    }
    this.queue = [];
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
