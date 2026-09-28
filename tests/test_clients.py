import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from clients.build import build

ROOT = Path(__file__).resolve().parents[1]


class ClientTests(unittest.TestCase):
    def test_archives_are_extractable_and_macos_entrypoints_are_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = build(folder)
            self.assertEqual(len(paths), 2)
            for path in paths:
                with ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    for info in archive.infolist():
                        self.assertNotIn('..', Path(info.filename).parts)
                        if info.filename.endswith('.command') or '/MacOS/' in info.filename:
                            self.assertTrue((info.external_attr >> 16) & 0o111)
                        if info.filename.endswith('.ps1'):
                            self.assertTrue(archive.read(info).startswith(b'\xef\xbb\xbf'))
                        if info.filename.endswith('Info.plist'):
                            self.assertEqual(plistlib.loads(archive.read(info))['CFBundleExecutable'], 'CFDControlRoom')

    def test_macos_launcher_opens_browser_and_closes_its_ssh(self):
        for path in (ROOT / 'clients/macos').iterdir():
            if path.name != 'Info.plist':
                result = subprocess.run(['/bin/bash', '-n', str(path)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory() as folder:
            stub = Path(folder)
            launcher = stub / 'launch.command'
            shutil.copyfile(ROOT / 'clients/macos/launch.command', launcher)
            (stub / 'ssh-hosts.sh').write_text('ssh_config_hosts() { printf "alpha\\nlab\\n"; }\n')
            (stub / 'ssh').write_text('#!/bin/sh\nprintf "SSH:%s\\n" "$@"\n: > "$CFD_SSH_READY"\nexec /bin/sleep 30\n')
            (stub / 'nc').write_text('#!/bin/sh\ntest -f "$CFD_SSH_READY"\n')
            (stub / 'open').write_text('#!/bin/sh\nprintf "OPEN:%s\\n" "$1"\n')
            for path in stub.iterdir():
                path.chmod(0o700)
            result = subprocess.run(['/bin/bash', str(launcher)],
                                    env=dict(os.environ, PATH=str(stub) + ':' + os.environ['PATH'],
                                             CFD_SSH_READY=str(stub / 'ready')), input='2\n\n',
                                    capture_output=True, text=True, timeout=5)
            self.assertIn('OPEN:http://127.0.0.1:8766', result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('SSH:lab\n', result.stdout)
            self.assertNotIn('SSH:alpha\n', result.stdout)
            self.assertNotIn('SSH:-p\n', result.stdout)
            self.assertNotIn('SSH:-l\n', result.stdout)
            self.assertIn('SSH:127.0.0.1:8766:127.0.0.1:8766', result.stdout)

    def test_ssh_config_lists_aliases_includes_and_ignores_patterns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').write_text('''# Host commented
Host alpha lab * !excluded *.example
  HostName example.test
  Port 2222
  User remote-user
  IdentityFile ~/.ssh/key
Host=quoted
hOsT "quoted" second # trailing comment
Include "config parts/*.conf" missing.conf
Match exec "do-not-execute"
''')
            (root / 'config parts').mkdir()
            (root / 'config parts/a.conf').write_text('Host jump alias-two\nInclude config\n')
            command = ['bash', '-c', 'source "$1"; ssh_config_root="$2"; ssh_config_hosts "$2/config" | sort -u',
                       'config-test', str(ROOT / 'clients/macos/ssh-hosts.sh'), str(root)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['alias-two', 'alpha', 'jump', 'lab', 'quoted', 'second'])
