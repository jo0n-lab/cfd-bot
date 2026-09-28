"""Build dependency-free Windows and macOS SSH launcher archives."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    bundles = {
        'Windows': [('windows/Start CFD.cmd', 'Start CFD.cmd', False),
                    ('windows/cfd-client.ps1', 'cfd-client.ps1', False)],
        'macOS': [('macos/Info.plist', 'CFD Control Room.app/Contents/Info.plist', False),
                  ('macos/CFDControlRoom', 'CFD Control Room.app/Contents/MacOS/CFDControlRoom', True),
                  ('macos/launch.command', 'CFD Control Room.app/Contents/Resources/launch.command', True),
                  ('macos/ssh-hosts.sh', 'CFD Control Room.app/Contents/Resources/ssh-hosts.sh', False)],
    }
    files = []
    for platform, entries in bundles.items():
        destination = output / f'CFD-Control-Room-{platform}.zip'
        with ZipFile(destination, 'w', ZIP_DEFLATED) as archive:
            for source, name, executable in entries + [('README.md', 'README.txt', False)]:
                text = (ROOT / source).read_text()
                # Windows PowerShell 5.1 detects Unicode scripts reliably by BOM.
                data = (text.replace('\n', '\r\n').encode('utf-8-sig') if name.endswith('.ps1')
                        else text.replace('\n', '\r\n').encode('utf-8') if name.endswith('.cmd')
                        else text.encode('utf-8'))
                info = ZipInfo(f'CFD-Control-Room-{platform}/{name}', date_time=(2026, 9, 28, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if executable else 0o100644) << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, data)
        files.append(destination)
    return files


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT.parent / 'cfd_bot/web_static/downloads')
    for path in build(parser.parse_args().output):
        print(path)
