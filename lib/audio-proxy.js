'use strict';

const crypto = require('crypto');
const http = require('http');
const https = require('https');
const {URL} = require('url');

function allowedUrl(value) {
  let parsed;
  try { parsed = new URL(value); } catch (_) { return null; }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password ||
      (parsed.port && parsed.port !== '443') ||
      !/^[a-zA-Z0-9.-]+$/.test(parsed.hostname) ||
      !/(^|\.)(yandex\.net|yandex\.ru)$/.test(parsed.hostname)) return null;
  return parsed;
}

function AudioProxy() {
  this.server = null;
  this.targets = new Map();
}

AudioProxy.prototype.start = function () {
  if (this.server && this.server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => this.handle(req, res));
    this.server = server;
    server.once('error', error => {
      if (this.server === server) this.server = null;
      reject(error);
    });
    server.listen(6601, '127.0.0.1', () => resolve());
  });
};

AudioProxy.prototype.createUrl = async function (target, codec, transport, key) {
  const parsed = allowedUrl(target);
  const mode = transport || 'raw';
  if (!parsed || codec !== 'flac' || !['raw', 'encraw'].includes(mode) ||
      (mode === 'encraw' && (typeof key !== 'string' || !/^[a-f0-9]{32}$/i.test(key)))) {
    throw new Error('Некорректный адрес FLAC-потока');
  }
  await this.start();
  const now = Date.now();
  for (const [id, entry] of this.targets) {
    if (entry.expires < now) this.targets.delete(id);
  }
  const id = crypto.randomBytes(18).toString('hex');
  this.targets.set(id, {url: parsed, codec, transport: mode,
    key: mode === 'encraw' ? Buffer.from(key, 'hex') : null, expires: now + 2 * 60 * 60 * 1000});
  return 'http://127.0.0.1:6601/' + id + '.flac';
};

AudioProxy.prototype.handle = function (req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405, {'Allow': 'GET, HEAD'});
    return res.end();
  }
  const match = /^\/([a-f0-9]{36})\.flac$/.exec((req.url || '').split('?')[0]);
  const entry = match && this.targets.get(match[1]);
  if (!entry || entry.expires < Date.now()) {
    res.writeHead(404);
    return res.end();
  }
  const headers = {};
  if (req.headers.range && /^bytes=\d*-\d*$/.test(req.headers.range)) {
    headers.Range = req.headers.range;
  }
  const range = /^bytes=(\d+)-/.exec(headers.Range || '');
  const rangeStart = range ? Number(range[1]) : 0;
  const upstream = https.request({
    protocol: entry.url.protocol,
    hostname: entry.url.hostname,
    port: 443,
    path: entry.url.pathname + entry.url.search,
    method: req.method,
    headers
  }, response => {
    const forwarded = {};
    ['accept-ranges', 'content-length', 'content-range', 'etag', 'last-modified'].forEach(name => {
      if (response.headers[name]) forwarded[name] = response.headers[name];
    });
    forwarded['content-type'] = 'audio/flac';
    res.writeHead(response.statusCode || 502, forwarded);
    if (req.method === 'HEAD') return res.end();
    if (entry.transport !== 'encraw') return response.pipe(res);
    const effectiveStart = response.statusCode === 206 ? rangeStart : 0;
    const counter = Buffer.alloc(16);
    counter.writeBigUInt64BE(BigInt(Math.floor(effectiveStart / 16)), 8);
    const decipher = crypto.createDecipheriv('aes-128-ctr', entry.key, counter);
    const skip = effectiveStart % 16;
    let first = true;
    response.on('data', chunk => {
      let decoded = decipher.update(chunk);
      if (first && skip) decoded = decoded.subarray(skip);
      first = false;
      if (decoded.length && !res.write(decoded)) response.pause();
    });
    res.on('drain', () => response.resume());
    response.on('end', () => {
      const tail = decipher.final();
      if (tail.length) res.write(tail);
      res.end();
    });
    response.on('error', () => res.destroy());
  });
  upstream.setTimeout(30000, () => upstream.destroy(new Error('FLAC upstream timeout')));
  upstream.on('error', () => {
    if (!res.headersSent) res.writeHead(502);
    res.end();
  });
  res.on('close', () => upstream.destroy());
  req.pipe(upstream);
};

AudioProxy.prototype.stop = function () {
  this.targets.clear();
  if (!this.server) return Promise.resolve();
  const server = this.server;
  this.server = null;
  return new Promise(resolve => server.close(() => resolve()));
};

module.exports = AudioProxy;
