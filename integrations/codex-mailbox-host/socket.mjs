import net from 'node:net';
import WebSocket from 'ws';

export function connect(socketPath) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket('ws://localhost/rpc', {
      createConnection: () => net.createConnection(socketPath), handshakeTimeout: 5000,
    });
    ws.once('error', reject);
    ws.once('open', () => { ws.removeListener('error', reject); resolve(ws); });
  });
}
