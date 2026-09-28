import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TunnelTests(unittest.TestCase):
    def test_tunnel_keeps_ports_and_ssh_options_separate(self):
        script = Path(__file__).resolve().parents[1] / 'bin/cfd-web-tunnel'
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder) / 'ssh'
            stub.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n')
            stub.chmod(0o700)
            result = subprocess.run(['/bin/sh', str(script), 'user@host', '18765', '8765', '-p', '2222'],
                                    env=dict(os.environ, PATH=folder), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('<127.0.0.1:18765:127.0.0.1:8765>', result.stdout)
            self.assertIn('<-p>\n<2222>\n<user@host>', result.stdout)
            self.assertIn('<ExitOnForwardFailure=yes>', result.stdout)
            for port in ('0', '65536', 'abc', '8765;touch /tmp/never'):
                result = subprocess.run(['/bin/sh', str(script), 'user@host', port],
                                        env=dict(os.environ, PATH=folder), capture_output=True)
                self.assertEqual(result.returncode, 2)
