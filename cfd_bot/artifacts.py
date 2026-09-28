"""Case-declared exports and immutable completion attachments."""
import glob
import shutil
from pathlib import Path

from .config import inside
from .ui import load_ui

MAX_DOCUMENT = 49 * 1024 * 1024
MAX_PHOTO = 9 * 1024 * 1024


def export_files(case, export, since=None):
    root = Path(case['_root'])
    found = []
    for name in glob.iglob(str(root / export['pattern']), recursive=True):
        path = inside(root, str(Path(name).relative_to(root)))
        try:
            stat = path.stat()
            if not path.is_file() or (since is not None and stat.st_mtime < since):
                continue
            found.append((stat.st_mtime, path))
        except FileNotFoundError:
            continue
    return [p for _, p in sorted(found, key=lambda x: (x[0], str(x[1])), reverse=True)[:export['max_files']]]


def freeze_exports(case, folder, since, status=None):
    ui = load_ui(case.get('_ui_dir'))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    files, notes = [], []
    try:
        files.extend(residual_files(case, folder / 'residual', since))
    except (OSError, ValueError) as exc:
        notes.append(ui.text('scenarios.artifacts.residual_error', error=exc))
    for export in case['exports']:
        if not export['on_complete'] or (status is not None and status not in export.get(
                'on', ['succeeded', 'failed', 'interrupted'])):
            continue
        try:
            sources = export_files(case, export, since)
            if not sources:
                notes.append(ui.text('scenarios.artifacts.missing', name=export['name']))
            for i, source in enumerate(sources):
                if source.stat().st_size > MAX_DOCUMENT:
                    notes.append(ui.text('scenarios.artifacts.too_large', name=export['name']))
                    continue
                dest = folder / f"{export['name']}-{i}{source.suffix}"
                shutil.copy2(source, dest)
                files.append(dict(path=str(dest), kind=export['kind'], caption=ui.text(
                    'scenarios.artifacts.caption', case_name=case['name'], name=export['name'])))
        except (OSError, ValueError) as exc:
            notes.append(ui.text('scenarios.artifacts.export_error', name=export['name'], error=exc))
    return files, notes


def residual_files(case, folder=None, since=None):
    """Use the case-owned PNG; optionally freeze it before another run starts."""
    ui = load_ui(case.get('_ui_dir'))
    pattern = case.get('residual_pattern')
    if not pattern:
        return []
    paths = export_files(case, dict(pattern=pattern, max_files=1), since)
    if not paths:
        return []
    source = paths[0]
    if source.suffix.lower() != '.png':
        raise ValueError(ui.text('scenarios.artifacts.residual_extension'))
    if source.stat().st_size > MAX_DOCUMENT:
        raise ValueError(ui.text('scenarios.artifacts.residual_too_large'))
    if folder is not None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = folder / 'residual.png'
        shutil.copy2(source, destination)
    else:
        destination = source
    return [dict(path=str(destination), kind='photo', caption=ui.text(
        'scenarios.artifacts.residual_caption', case_name=case['name'], filename=source.name))]
