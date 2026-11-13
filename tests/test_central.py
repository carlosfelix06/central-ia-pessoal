import os
import tempfile
import unittest
from unittest.mock import patch
from central.core import AGENTS, Store, Orchestrator
from central.providers import MockProvider, APIProvider


class CentralTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.grants = {(a, 'draft') for a in AGENTS}

    def tearDown(self):
        self.store.close()

    def app(self, provider=None, **kwargs):
        return Orchestrator(self.store, provider or MockProvider(), grants=self.grants, **kwargs)

    @patch('urllib.request.urlopen', side_effect=AssertionError('Rede proibida'))
    def test_six_agents_offline(self, network):
        result = self.app().run('Preparar minha semana', list(AGENTS))
        self.assertEqual(set(result), set(AGENTS))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0], 12)
        network.assert_not_called()

    def test_routing(self):
        self.assertEqual(set(self.app().run('Revisar currículo e orçamento')), {'analista_bi', 'analytics_finops'})

    def test_external_action_denied(self):
        with self.assertRaises(PermissionError):
            self.app().run('Enviar mensagem', action='send')
        self.assertEqual(self.store.db.execute('SELECT state FROM tasks').fetchone()[0], 'failed')

    def test_grants_default_deny(self):
        with self.assertRaises(PermissionError):
            Orchestrator(self.store, MockProvider()).run('Olá')

    @patch.object(APIProvider, 'generate')
    def test_live_default_denied(self, generate):
        with self.assertRaises(PermissionError):
            self.app(APIProvider('openai')).run('Olá')
        generate.assert_not_called()

    def test_direct_adapter_denied(self):
        with patch.dict(os.environ, {'CENTRAL_ALLOW_PAID': 'false'}):
            with self.assertRaises(PermissionError):
                APIProvider('openai').generate('Coordenador de BI', 'Olá')

    def test_budget_limits(self):
        self.store.reserve('a', 5, 10, 5)
        self.store.reserve('b', 5, 10, 5)
        with self.assertRaises(PermissionError):
            self.store.reserve('c', 1, 10, 5)
        with self.assertRaises(PermissionError):
            self.store.reserve('d', 6, 100, 5)

    def test_failure_keeps_reservation(self):
        with patch.object(APIProvider, 'generate', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.app(APIProvider('xai'), allow_paid=True, reserve_microusd=5,
                         daily_microusd=10, call_microusd=5).run('Olá')
        self.assertEqual(self.store.db.execute('SELECT SUM(amount) FROM charges').fetchone()[0], 5)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/test.sqlite3'
            one = Store(path)
            Orchestrator(one, MockProvider(), grants=self.grants).run('Olá')
            one.close()
            two = Store(path)
            self.assertEqual(two.db.execute('SELECT state FROM tasks').fetchone()[0], 'done')
            two.close()

    @patch('urllib.request.urlopen')
    def test_adapter_contracts(self, network):
        import json
        for name in ('openai', 'gemini', 'xai'):
            payload = ({'choices': [{'message': {'content': 'ok'}}]} if name == 'gemini'
                       else {'output': [{'content': [{'type': 'output_text', 'text': 'ok'}]}]})
            import io
            network.return_value.__enter__.return_value = io.StringIO(json.dumps(payload))
            with patch.dict(os.environ, {'CENTRAL_ALLOW_PAID': 'true',
                name.upper() + '_API_KEY': 'fake-test-key', name.upper() + '_MODEL': 'fake-model'}):
                self.assertEqual(APIProvider(name).generate('Especialista DataViz', 'Oi'), 'ok')
            request = network.call_args.args[0]
            self.assertEqual(json.loads(request.data)['model'], 'fake-model')

    def test_invalid_input(self):
        for text, agents in [('', None), ('x', ['desconhecido']), ('x', [])]:
            with self.assertRaises(ValueError):
                self.app().run(text, agents)


if __name__ == '__main__':
    unittest.main()
