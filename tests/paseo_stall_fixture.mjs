import fs from 'node:fs';
import http from 'node:http';
process.title = 'Paseo Daemon';
http.createServer((req, res) => res.end('ok')).listen(Number(process.env.TEST_PORT), '127.0.0.1');
setTimeout(() => {
  fs.writeFileSync(process.env.TEST_FIFO + '.started', 'ready');
  // The test writer releases this read after 1.8 seconds.
  fs.readFileSync(process.env.TEST_FIFO);
}, 3000);
