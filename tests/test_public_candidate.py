"""Offline contract tests; all state stays in temporary directories."""
import asyncio
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import shutil
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hunt_teams import load_teams, save_teams
from leader_shout import save_settings, load_settings, chat_body
from managed_leader_state import identity, publish, read, recent_position
from protocol_auth import load_adapter, checked_packet
from public_policy import install_policy
from flask import Flask, jsonify
from tools.init_local import initialize
from tools.check_release import scan_file
from process_scope import owns_process
from version_profile import configured_host


def team(identifier, leader, member):
    return dict(format=1, id=identifier, name=identifier, enabled=True,
                leader=leader, members=[dict(account=member)], mode='single', scene_ids=[20])


class PublicContracts(unittest.TestCase):
    def test_process_scope_rejects_other_installation(self):
        import subprocess
        with tempfile.TemporaryDirectory(prefix='controller space ') as directory:
            base = Path(directory)
            expected = base / 'simple_bot.py'
            command = subprocess.list2cmdline([sys.executable, '-u', str(expected), '--account', 'example'])
            row = dict(ExecutablePath=sys.executable, CommandLine=command)
            self.assertTrue(owns_process(row, base))
            row['CommandLine'] = subprocess.list2cmdline([sys.executable, '-u', str(base / 'other' / 'simple_bot.py'), '--account', 'example'])
            self.assertFalse(owns_process(row, base))
            self.assertFalse(owns_process(dict(ExecutablePath='', CommandLine=command), base))
            self.assertFalse(owns_process(dict(ExecutablePath=str(base / 'other' / 'TLBBBotEngine.exe')), base))
            self.assertTrue(owns_process(dict(ExecutablePath=str(base / 'TLBBBotEngine.exe')), base))

    def test_supported_hosts_are_explicit_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.assertFalse(configured_host(base, 'example.test'))
            path = base / 'server_config.json'
            path.write_text(json.dumps({'supported_login_hosts': ['example.test']}), encoding='utf-8')
            self.assertTrue(configured_host(base, 'example.test'))
            self.assertFalse(configured_host(base, 'another.test'))
            path.write_text('{broken', encoding='utf-8')
            self.assertFalse(configured_host(base, 'example.test'))

    def test_teams_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'teams.json'
            plans = [team('first', 'example_a', 'example_b'), team('second', 'example_c', 'example_d')]
            save_teams(path, plans)
            self.assertEqual(load_teams(path), plans)

    def test_account_cannot_occupy_two_teams(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'teams.json'
            save_teams(path, [team('first', 'example_a', 'example_b'), team('second', 'example_c', 'example_b')])
            with self.assertRaises(ValueError):
                load_teams(path)

    def test_shouts_remain_per_leader(self):
        with tempfile.TemporaryDirectory() as directory:
            for account, message in [('example_a', '第一队'), ('example_b', '第二队')]:
                save_settings(directory, dict(enabled=True, leader=account, text=message,
                                              channel='world', interval_seconds=60))
            self.assertEqual(load_settings(directory, 'example_a')['text'], '第一队')
            self.assertEqual(load_settings(directory, 'example_b')['text'], '第二队')

    def test_team_chat_requires_real_context(self):
        with self.assertRaises(ValueError):
            chat_body('team', '测试')
        self.assertIsInstance(chat_body('team', '测试', {'team': 123}), bytes)

    def test_leader_state_rejects_stale_or_wrong_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = identity('localhost', 6500, 'first', 'example_a', 123)
            publish(directory, expected, 20, True, 1, 100, [10, 20], 100)
            self.assertEqual(recent_position(read(directory, expected, 102), 102), (10, 20))
            self.assertIsNone(read(directory, expected, 111))
            other = dict(expected, guid=124)
            self.assertIsNone(read(directory, other, 102))

    def test_auth_module_contract_and_bytes(self):
        module = types.ModuleType('test_auth_adapter')
        module.build_hello = lambda account: b'123456'
        module.build_auth = lambda account, challenge: b'123456'
        with patch.dict(sys.modules, test_auth_adapter=module):
            self.assertEqual(checked_packet(load_adapter('test_auth_adapter').build_hello('example')), b'123456')
        with self.assertRaises(RuntimeError):
            load_adapter('../secrets')
        with self.assertRaises(RuntimeError):
            checked_packet('123456')

    def test_missing_auth_fails_before_network(self):
        import simple_bot
        with patch('simple_bot.asyncio.open_connection') as network:
            dummy = types.SimpleNamespace(auth_profile='nonexistent_test_auth_module')
            with self.assertRaises(RuntimeError):
                asyncio.run(simple_bot.SimpleBot.login(dummy))
            network.assert_not_called()

    def test_defaults_block_all_game_start_paths(self):
        app = Flask(__name__)
        install_policy(app, lambda: {})
        for i, path in enumerate(('/api/bots/example/start', '/api/hunt-plan/start', '/api/hunt-teams/example/start')):
            app.add_url_rule(path, f'route_{i}', lambda: jsonify(ok=True), methods=['POST'])
        with app.test_client() as client:
            for path in ('/api/bots/example/start', '/api/hunt-plan/start', '/api/hunt-teams/example/start'):
                self.assertEqual(client.post(path).status_code, 409)

    def test_creation_block_and_explicit_boolean(self):
        app = Flask(__name__)
        config = {'enable_database_writes': 'true'}
        install_policy(app, lambda: config)
        app.add_url_rule('/api/bots/batch-create', 'api_batch_create_bots', lambda: jsonify(ok=True), methods=['POST'])
        with app.test_client() as client:
            self.assertEqual(client.post('/api/bots/batch-create').status_code, 409)
            config['enable_database_writes'] = True
            self.assertEqual(client.post('/api/bots/batch-create').status_code, 200)

    def test_init_keeps_existing_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'examples').mkdir()
            (base / 'examples/server_config.example.json').write_text('{"db_password":""}', encoding='utf-8')
            initialize(base)
            (base / 'server_config.json').write_text('{"private":"preserve"}', encoding='utf-8')
            self.assertEqual(initialize(base), [])
            self.assertEqual(json.loads((base / 'server_config.json').read_text())['private'], 'preserve')

    def test_release_scanner_detects_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            file = base / 'example.json'
            # Constructed dummy secret; no actual credential is stored in tests.
            file.write_text(json.dumps({'db_' + 'password': 'dummy' + '-not-a-real-password'}), encoding='utf-8')
            self.assertTrue(scan_file(file, base))
            file.write_text('{"db_password":""}', encoding='utf-8')
            self.assertEqual(scan_file(file, base), [])
            capture = base / ('private.' + 'pcapng')
            capture.write_text('dummy')
            self.assertTrue(scan_file(capture, base))


class WebPreview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='tlbb_public_web_')
        cls.base = Path(cls.temp.name)
        for source in ROOT.glob('*.py'):
            shutil.copy2(source, cls.base / source.name)
        shutil.copytree(ROOT / 'templates', cls.base / 'templates')
        shutil.copytree(ROOT / 'examples', cls.base / 'examples')
        initialize(cls.base)
        spec = importlib.util.spec_from_file_location('public_web_test', cls.base / 'web_console.py')
        cls.web = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.web
        with patch('pymysql.connect', side_effect=AssertionError('No database access in preview')):
            spec.loader.exec_module(cls.web)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
        sys.modules.pop('public_web_test', None)

    def test_real_web_page_and_empty_team_api(self):
        with patch('pymysql.connect', side_effect=AssertionError('No database access in preview')):
            with self.web.app.test_client() as client:
                self.assertEqual(client.get('/').status_code, 200)
                response = client.get('/api/hunt-teams')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['teams'], [])
                config = client.get('/api/server-config').json
                self.assertEqual(config['db_password'], '')
                self.assertIs(config['enable_database_writes'], False)

    def test_real_web_start_and_create_block_before_database(self):
        with patch('pymysql.connect', side_effect=AssertionError('No database access in preview')):
            with self.web.app.test_client() as client:
                for path in ('/api/bots/example/start', '/api/hunt-plan/start', '/api/bots/batch-create'):
                    self.assertEqual(client.post(path, json={}).status_code, 409)


if __name__ == '__main__':
    unittest.main()
