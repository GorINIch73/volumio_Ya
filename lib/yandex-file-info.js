'use strict';

// Native Node HTTPS transport for /get-file-info. Axios in the reference
// plugin uses this same Node TLS stack; keep the request headers aligned.
const https = require('https');
const zlib = require('zlib');

function decodeBody(body, encoding) {
  if (!encoding || encoding === 'identity') return body;
  if (encoding === 'gzip' || encoding === 'x-gzip') return zlib.gunzipSync(body);
  if (encoding === 'deflate') {
    try { return zlib.inflateSync(body); } catch (_) { return zlib.inflateRawSync(body); }
  }
  if (encoding === 'br' && zlib.brotliDecompressSync) return zlib.brotliDecompressSync(body);
  throw new Error('unsupported content encoding');
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  input += chunk;
  if (input.length > 32768) process.exit(2);
});
process.stdin.on('end', () => {
  let request;
  try {
    request = JSON.parse(input);
    if (request.host !== 'api.music.yandex.net' || typeof request.path !== 'string' ||
        !request.path.startsWith('/get-file-info?') || typeof request.token !== 'string' ||
        !/^[A-Za-z0-9._/-]{1,120}$/.test(request.clientId || '')) {
      throw new Error('invalid request');
    }
  } catch (_) {
    process.stderr.write('invalid-request\n');
    process.exitCode = 2;
    return;
  }

  const headers = {
    Accept: 'application/json, text/plain, */*',
    Authorization: 'OAuth ' + request.token,
    'Accept-Language': 'ru',
    'User-Agent': 'axios/0.27.2',
    'X-Yandex-Music-Client': request.clientId,
    'Accept-Encoding': 'gzip, compress, deflate' +
      (typeof zlib.brotliDecompressSync === 'function' ? ', br' : '')
  };
  const outgoing = https.request({
    hostname: request.host,
    method: 'GET',
    path: request.path,
    headers,
    timeout: 10000
  }, response => {
    const chunks = [];
    let size = 0;
    response.on('data', chunk => {
      size += chunk.length;
      if (size > 8 * 1024 * 1024) outgoing.destroy(new Error('response too large'));
      else chunks.push(chunk);
    });
    response.on('end', () => {
      try {
        const raw = decodeBody(Buffer.concat(chunks), String(response.headers['content-encoding'] || '').toLowerCase());
        if (raw.length > 8 * 1024 * 1024) throw new Error('response too large');
        process.stdout.write(JSON.stringify({
          status: response.statusCode,
          headers: response.headers,
          body: raw.toString('utf8')
        }) + '\n');
      } catch (_) {
        process.stderr.write('response-decode-failed\n');
        process.exitCode = 1;
      }
    });
  });
  outgoing.on('timeout', () => outgoing.destroy(new Error('timeout')));
  outgoing.on('error', () => {
    // Never forward errors: they can contain the signed URL or credentials.
    process.stderr.write('https-request-failed\n');
    process.exitCode = 1;
  });
  outgoing.end();
});
