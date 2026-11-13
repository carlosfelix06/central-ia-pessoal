from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import patch
from central.core import Store
from central.workflow import Workflow, CHAIN, markdown_report

PROPOSAL = {'mensal_brl': '19000.00', 'cidade': 'Matriz-SP', 'contrato': 'PJ'}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.flow = Workflow(self.store)

    def tearDown(self):
        self.store.close()

    def create(self, **limits):
        return self.flow.create('test', PROPOSAL, **limits)

    @patch('socket.socket', side_effect=AssertionError('Rede proibida'))
    @patch('central.providers.APIProvider.generate', side_effect=AssertionError('API proibida'))
    def test_full_chain_offline_and_exact_handoffs(self, api, network):
        result = self.flow.execute(self.create())
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['executions'], 4)
        self.assertEqual([s['agent'] for s in result['steps']], list(CHAIN))
        for previous, current in zip(result['steps'], result['steps'][1:]):
            self.assertEqual(previous['result'], current['received'])
        self.assertEqual(result['steps'][2]['result']['saldo_ilustrativo_brl'], '13700.00')
        self.assertEqual(result['steps'][-1]['result']['acoes_externas_executadas'], [])
        api.assert_not_called()
        network.assert_not_called()

    def test_messages_states_decisions_audited(self):
        result = self.flow.execute(self.create())
        self.assertEqual(len(result['events']), 9)
        for step in result['steps']:
            self.assertEqual(step['state'], 'done')
            matching = [e for e in result['events'] if e['agent'] == step['agent'] and e['state'] == 'done']
            self.assertEqual(matching[0]['detail']['resultado'], step['result'])
            self.assertTrue(matching[0]['detail']['decisao'])

    def test_transient_failure_recovers(self):
        self.flow.failures['analista_bi'] = 1
        result = self.flow.execute(self.create())
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['executions'], 5)
        self.assertEqual(result['steps'][1]['attempts'], 2)
        self.assertEqual(len([e for e in result['events'] if e['state'] == 'retryable']), 1)

    def test_retry_exhaustion_blocks_downstream(self):
        self.flow.failures['analista_bi'] = 99
        result = self.flow.execute(self.create(max_attempts=2))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['steps'][1]['attempts'], 2)
        self.assertEqual(result['steps'][2]['attempts'], 0)
        self.assertEqual(self.flow.execute('test'), result)

    def test_permanent_failure_not_retried(self):
        key = self.create()
        with patch('central.workflow.process', side_effect=ValueError('malformado')):
            result = self.flow.execute(key)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['executions'], 1)

    def test_global_execution_limit(self):
        result = self.flow.execute(self.create(max_executions=2))
        self.assertEqual(result['state'], 'limited')
        self.assertEqual(result['executions'], 2)
        self.assertEqual(result['steps'][2]['attempts'], 0)

    def test_tick_limit_resume(self):
        key = self.create()
        partial = self.flow.execute(key, max_ticks=1)
        self.assertEqual(partial['executions'], 1)
        self.assertEqual(self.flow.execute(key)['executions'], 4)

    def test_cooperative_deadline(self):
        key = self.create()
        with patch('central.workflow.time.monotonic', side_effect=[0, 11]):
            partial = self.flow.execute(key)
        self.assertEqual(partial['executions'], 0)
        self.assertEqual(self.flow.execute(key)['state'], 'done')

    def test_idempotent_create_and_execute(self):
        key = self.create()
        result = self.flow.execute(key)
        self.assertEqual(self.create(), key)
        self.assertEqual(self.flow.execute(key), result)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM workflow_steps').fetchone()[0], 4)

    def test_key_conflict(self):
        self.create()
        with self.assertRaises(ValueError):
            self.flow.create('test', dict(PROPOSAL, mensal_brl='20000'))
        with self.assertRaises(ValueError):
            self.flow.create('test', PROPOSAL, max_attempts=2)

    def test_retry_checkpoint_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/flow.sqlite3'
            one = Store(path)
            flow = Workflow(one, failures={'analista_bi': 1})
            key = flow.create('persist', PROPOSAL)
            partial = flow.execute(key, max_ticks=2)
            self.assertEqual(partial['state'], 'retryable')
            one.close()
            two = Store(path)
            try:
                result = Workflow(two).execute(key)
                self.assertEqual(result['state'], 'done')
                self.assertEqual(result['executions'], 5)
                self.assertEqual(result['steps'][0]['attempts'], 1)
            finally:
                two.close()

    def test_atomic_rollback_on_process_interruption(self):
        key = self.create()
        with patch('central.workflow.process', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.flow.execute(key)
        self.assertEqual(self.flow.snapshot(key)['executions'], 0)
        self.assertEqual(self.flow.execute(key)['state'], 'done')

    def test_two_workers_no_duplicate_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/flow.sqlite3'
            store = Store(path)
            Workflow(store).create('same', PROPOSAL)
            store.close()
            def worker():
                local = Store(path)
                try:
                    return Workflow(local).execute('same')
                finally:
                    local.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: worker(), range(2)))
            self.assertTrue(all(r['state'] == 'done' for r in results))
            self.assertTrue(all(r['executions'] == 4 for r in results))
            self.assertTrue(all(s['attempts'] == 1 for s in results[-1]['steps']))

    def test_invalid_limits_and_proposal(self):
        for limits in ({'max_attempts': 0}, {'max_attempts': 6}, {'max_executions': 21}):
            with self.assertRaises(ValueError):
                self.create(**limits)
        for amount in ('NaN', '-1', 'Infinity', '1000001'):
            with self.assertRaises(ValueError):
                self.flow.create('bad', dict(PROPOSAL, mensal_brl=amount))
        key = self.create()
        for limits in ({'max_ticks': 0}, {'max_ticks': 21}, {'max_seconds': 0}):
            with self.assertRaises(ValueError):
                self.flow.execute(key, **limits)

    def test_unknown_run(self):
        with self.assertRaises(KeyError):
            self.flow.execute('unknown')

    def test_report_matches_persisted_recovery(self):
        self.flow.failures['analista_bi'] = 1
        result = self.flow.execute(self.create())
        report = markdown_report(result)
        self.assertIn('Execuções de etapas: **5**', report)
        self.assertIn('Tentativas: 2', report)
        self.assertIn('TransientFailure', report)
        self.assertIn('13700.00', report)
        for step in result['steps']:
            self.assertIn(step['agent'], report)
