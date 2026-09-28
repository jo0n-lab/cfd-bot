import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from cfd_bot.bot import Bot
from cfd_bot.storage import Store
from cfd_bot.ui import DEFAULT_UI_DIR, UiCatalog, UiResourceError


ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_RUNTIME = [
    path for path in (ROOT / 'cfd_bot').glob('*.py')
    # These presentation adapters are never imported by the Telegram runtime.
    if path.name not in {'cli.py', 'gui.py', 'web.py'}
]


class FakeAPI:
    def __init__(self):
        self.messages = []

    def send(self, chat, text, keyboard=None):
        self.messages.append((chat, text, keyboard))
        return {'message_id': len(self.messages)}


class UiResourceTests(unittest.TestCase):
    def test_catalog_has_menu_and_scenario_namespaces(self):
        catalog = UiCatalog(DEFAULT_UI_DIR)
        self.assertTrue(catalog.has('menus.home.help'))
        self.assertTrue(catalog.has('menus.tickets.card.body'))
        self.assertTrue(catalog.has('scenarios.status.compact'))
        self.assertTrue(catalog.has('scenarios.notifications.external_started'))
        self.assertGreater(len(catalog.keys()), 400)

    def test_every_literal_resource_reference_exists(self):
        catalog = UiCatalog(DEFAULT_UI_DIR)
        referenced = set()
        for path in TELEGRAM_RUNTIME:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr in {'text', 'value', 'has'}:
                    if first.value.startswith(('menus.', 'scenarios.', 'strings.')):
                        referenced.add(first.value)
                if isinstance(node.func, ast.Attribute) and node.func.attr == 't':
                    referenced.add('menus.tickets.' + first.value)
        missing = sorted(key for key in referenced if not catalog.has(key))
        self.assertEqual(missing, [])

    def test_telegram_runtime_contains_no_embedded_korean_copy(self):
        offenders = []
        for path in TELEGRAM_RUNTIME:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r'[가-힣]', node.value):
                    offenders.append(f'{path.name}:{getattr(node, "lineno", 0)}')
        self.assertEqual(offenders, [])

    def test_home_menu_is_driven_by_custom_json_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ui_dir = root / 'ui'
            shutil.copytree(DEFAULT_UI_DIR, ui_dir)
            path = ui_dir / 'menus/home.json'
            body = json.loads(path.read_text(encoding='utf-8'))
            body['help'] = 'CUSTOM HOME'
            body['keyboard'][0][0]['text'] = 'CUSTOM BUTTON'
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
            config = {
                '_path': str(root / 'bot.json'), '_ui_dir': str(ui_dir), '_text_file': None,
                'cases': [], 'case_globs': [], 'telegram': {'allowed_user_ids': [], 'chat_ids': []},
                'scheduler': {'enabled': False}, 'ofps_command': ['ofps'],
            }
            api = FakeAPI()
            Bot(config, Store(root / 'state'), api).dispatch(1, 'home', 'request')
            self.assertEqual(api.messages[-1][1], 'CUSTOM HOME')
            self.assertEqual(api.messages[-1][2]['inline_keyboard'][0][0]['text'], 'CUSTOM BUTTON')

    def test_missing_key_and_bad_template_fail_loudly(self):
        catalog = UiCatalog(DEFAULT_UI_DIR)
        with self.assertRaisesRegex(UiResourceError, 'missing UI resource'):
            catalog.text('menus.missing.value')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'broken.json').write_text('{"text": "{missing"}', encoding='utf-8')
            with self.assertRaisesRegex(UiResourceError, 'invalid format template'):
                UiCatalog(root)

    def test_manifest_makes_missing_resources_a_startup_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'ui'
            shutil.copytree(DEFAULT_UI_DIR, root)
            path = root / 'menus/home.json'
            body = json.loads(path.read_text(encoding='utf-8'))
            del body['help']
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
            with self.assertRaisesRegex(UiResourceError, 'missing required UI resources: menus.home.help'):
                UiCatalog(root)


if __name__ == '__main__':
    unittest.main()
