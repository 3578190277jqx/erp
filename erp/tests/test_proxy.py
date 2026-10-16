import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class ProxyDeploymentTests(SimpleTestCase):
    def test_https_forwarded_by_explicitly_trusted_proxy_does_not_redirect_loop(self):
        # Exercises the real Waitress -> WSGI -> Django boundary, not a mocked header.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, DJANGO_DEBUG='false',
                   DJANGO_SECRET_KEY='proxy-test-only-ephemeral-secret-with-more-than-50-characters',
                   DJANGO_ALLOWED_HOSTS='127.0.0.1', DJANGO_TRUSTED_PROXY='127.0.0.1',
                   BIND_HOST='127.0.0.1', PORT=str(port), PYTHONUTF8='1')
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/serve.py')], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 6
            status = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(f'Waitress failed to start: {process.communicate()[1].decode(errors="replace")}')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                try:
                    connection.request('GET', '/login/', headers={'X-Forwarded-Proto': 'https'})
                    response = connection.getresponse()
                    status = response.status
                    response.read()
                    break
                except (ConnectionRefusedError, OSError):
                    time.sleep(.1)
                finally:
                    connection.close()
            self.assertEqual(status, 200, 'HTTPS proxy request must not redirect back to itself')
        finally:
            process.terminate()
            process.communicate(timeout=5)
