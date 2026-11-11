"""Loopback-only checks against the installed mihomo; no production reload or WAN."""
import contextlib
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest

import proxy_accounting as pa
import traffic_guard as tg

CORE = Path(os.environ.get('MIHOMO_BINARY',
                          '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'))
PAYLOAD = b'x' * (256 * 1024)


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        if self.request.recv(1) != b'g':
            return
        for index in range(0, len(PAYLOAD), 8192):
            self.request.sendall(PAYLOAD[index:index+8192])
            time.sleep(.005)
        with contextlib.suppress(OSError):
            self.request.recv(1)


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        header = b''
        while b'\r\n\r\n' not in header:
            data = self.request.recv(4096)
            if not data: return
            header += data
        if not header.startswith(b'CONNECT proxied.test:'):
            return
        with socket.create_connection(('127.0.0.1', self.server.echo_port), timeout=3) as upstream:
            self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            peers = [self.request, upstream]
            while True:
                ready, _, _ = select.select(peers, [], [], 3)
                if not ready: continue
                for source in ready:
                    data = source.recv(65536)
                    if not data: return
                    (upstream if source is self.request else self.request).sendall(data)


@unittest.skipUnless(CORE.exists(), 'Installed mihomo is needed for the local integration test')
class RealCoreChecks(unittest.TestCase):
    def test_proxy_payload_counted_and_identical_direct_payload_excluded(self):
        with tempfile.TemporaryDirectory(prefix='pt-', dir='/tmp') as temp:
            folder = Path(temp)
            echo = TCPServer(('127.0.0.1', 0), Echo)
            proxy = TCPServer(('127.0.0.1', 0), Proxy)
            proxy.echo_port = echo.server_address[1]
            for server in [echo, proxy]:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            probe = socket.socket(); probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]; probe.close()
            config = {
                'mode': 'rule', 'log-level': 'error', 'allow-lan': False,
                'external-controller-unix': str(folder/'api.sock'), 'secret': 'local-test',
                'listeners': [{'name': 'local-test', 'type': 'http', 'listen': '127.0.0.1', 'port': port}],
                'proxies': [{'name': 'TEST-PROXY', 'type': 'http', 'server': '127.0.0.1',
                             'port': proxy.server_address[1]}],
                'rules': ['DOMAIN,proxied.test,TEST-PROXY', 'MATCH,DIRECT'],
                'dns': {'enable': False}
            }
            (folder/'config.json').write_text(json.dumps(config))
            with open(folder/'core.log', 'w') as output:
                proc = subprocess.Popen([str(CORE), '-d', str(folder), '-f', str(folder/'config.json')],
                                        stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            (folder/'own-core.pid.json').write_text(json.dumps({'pid': proc.pid, 'executable': str(CORE)}))
            controller = tg.Controller(dict(controller_socket=str(folder/'api.sock'), controller_secret='local-test'))
            stop = threading.Event()
            errors = []
            sampler = None
            try:
                for _ in range(100):
                    if (folder/'api.sock').exists(): break
                    if proc.poll() is not None:
                        self.fail((folder/'core.log').read_text()[-2000:])
                    time.sleep(.05)
                meter = pa.new_meter(controller.counters(), dt.datetime.now(tg.ZONE))
                def collect():
                    while not stop.is_set():
                        try: pa.update(meter, controller.counters(), dt.datetime.now(tg.ZONE))
                        except Exception as e: errors.append(type(e).__name__); return
                        stop.wait(.1)
                sampler = threading.Thread(target=collect, daemon=True); sampler.start()
                for target in ['proxied.test', '127.0.0.1']:
                    with socket.create_connection(('127.0.0.1', port), timeout=4) as client:
                        client.settimeout(4)
                        address = f'{target}:{echo.server_address[1]}'
                        client.sendall(f'CONNECT {address} HTTP/1.1\r\nHost: {address}\r\n\r\n'.encode())
                        response = b''
                        while b'\r\n\r\n' not in response: response += client.recv(1)
                        self.assertIn(b' 200 ', response)
                        client.sendall(b'g')
                        received = b''
                        while len(received) < len(PAYLOAD):
                            data = client.recv(65536)
                            self.assertTrue(data)
                            received += data
                        self.assertEqual(received, PAYLOAD)
                        # Keep the tunnel alive so a final snapshot can classify
                        # every byte; the separate short-tail test covers closure.
                        time.sleep(.4)
                time.sleep(.2)
                stop.set(); sampler.join(3)
                self.assertEqual(errors, [])
                total = pa.totals(meter)
                self.assertEqual(meter['proxy']['download'], len(PAYLOAD))
                self.assertEqual(meter['direct']['download'], len(PAYLOAD))
                self.assertEqual(total['unknown_bytes'], 0)
                self.assertLess(total['budget_bytes'], len(PAYLOAD) + 100)
                print('REAL_CORE_LOOPBACK_OK', json.dumps(total))
            finally:
                stop.set()
                if sampler: sampler.join(3)
                controller.close()
                # Only this test's PID-file process may be terminated.
                owned = json.loads((folder/'own-core.pid.json').read_text())
                if proc.poll() is None:
                    check = subprocess.run(['ps', '-p', str(owned['pid']), '-o', 'uid=', '-o', 'command='],
                                           capture_output=True, text=True, check=True).stdout.strip()
                    uid, command = check.split(None, 1)
                    assert owned['pid'] == proc.pid > 1 and int(uid) == os.getuid()
                    assert command.startswith(str(CORE)+' -d '+str(folder)+' ')
                    proc.terminate()
                    try: proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill(); proc.wait(5)
                echo.shutdown(); proxy.shutdown()
                echo.server_close(); proxy.server_close()


if __name__ == '__main__':
    unittest.main()
