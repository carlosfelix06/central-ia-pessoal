from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from central.phase3.policy import Settings, AGENTS, canonical, cost_microusd, estimate
from central.phase3.queue import Queue
from central.phase3.transport import live_ready, openai_request, parse_response, request_body
from central.phase3.worker import Worker, validate_outcome


class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Spawn no Windows pode exceder 3s sob carga; os testes de timeout mantêm prazo explícito de 0,4s.
        self.settings = Settings(budget_microusd=100000, timeout_seconds=10)
        self.queue = Queue(self.temp.name, self.settings)
        # Nenhum teste usa rede real, nem se a mÃ¡quina jÃ¡ tiver credenciais configuradas.
        self.network = patch('http.client.HTTPSConnection', side_effect=AssertionError('Rede real proibida'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.queue.close()
        self.temp.cleanup()

    def submit(self, key='one', agents=('coordenador_bi',), text='Pedido fictÃ­cio de teste'):
        return self.queue.submit(key, text, agents)

    def completed(self):
        run = self.submit()
        job = self.queue.claim()
        self.queue.finish(job, {'code': 'ok', 'text': 'Rascunho fictÃ­cio', 'input_tokens': 100, 'output_tokens': 16})
        return run, job['id']

    def action(self):
        run, job = self.completed()
        return self.queue.propose_action('action-one', job, 'send_email', {'to': 'example@example.invalid', 'body': 'Rascunho'})

    def test_six_agents_handoff_through_supervised_mock(self):
        run = self.submit(agents=tuple(AGENTS))
        results = Worker(self.queue).drain()
        self.assertEqual(len(results), 6)
        self.assertTrue(all(j['state'] == 'done' for j in self.queue.status(run)))
        messages = self.queue.private_messages(run)
        for previous, current in zip(messages, messages[1:]):
            received = json.loads(current['input'])
            self.assertEqual(received['resultado_anterior'], previous['output'])
            self.assertEqual(received['agente_anterior'], previous['agent'])
            self.assertEqual(received['pedido_original'], messages[0]['input'])

    def test_zero_budget_blocks_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            q = Queue(folder, Settings())
            try:
                run = q.submit('zero', 'Teste', ('coordenador_bi',))
                with patch('multiprocessing.get_context', side_effect=AssertionError('NÃ£o despachar')):
                    self.assertIsNone(Worker(q).once())
                self.assertEqual(q.status(run)[0]['state'], 'budget_blocked')
                self.assertEqual(q.db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 0)
            finally:
                q.close()

    def test_exact_budget_blocks_next_request(self):
        text = 'Teste'
        amount = estimate(self.settings, 'coordenador_bi', text)['reserve_microusd']
        with tempfile.TemporaryDirectory() as folder:
            q = Queue(folder, replace(self.settings, budget_microusd=amount))
            try:
                one = q.submit('one', text, ('coordenador_bi',))
                two = q.submit('two', text, ('coordenador_bi',))
                Worker(q).drain()
                self.assertEqual(q.status(one)[0]['state'], 'done')
                self.assertEqual(q.status(two)[0]['state'], 'budget_blocked')
                self.assertEqual(q.budget()['remaining_microusd'], 0)
            finally:
                q.close()

    def test_per_call_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            q = Queue(folder, replace(self.settings, call_microusd=1))
            try:
                run = q.submit('x', 'Teste', ('coordenador_bi',))
                self.assertIsNone(q.claim())
                self.assertEqual(q.status(run)[0]['state'], 'budget_blocked')
            finally:
                q.close()

    def test_overrun_opens_persistent_circuit(self):
        run = self.submit()
        job = self.queue.claim()
        self.queue.finish(job, {'code': 'ok', 'text': 'x', 'input_tokens': 200000, 'output_tokens': 1})
        self.assertTrue(self.queue.budget()['paused'])
        self.assertEqual(self.queue.status(run)[0]['state'], 'uncertain')
        second = self.submit('two')
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.status(second)[0]['state'], 'budget_blocked')

    def test_retry_backoff_and_attempt_limit(self):
        run = self.submit()
        worker = Worker(self.queue)
        for attempt in range(2):
            self.assertEqual(worker.once(simulation='rate_limited')['outcome'], 'rate_limited')
            if attempt == 0:
                self.assertEqual(self.queue.status(run)[0]['state'], 'retryable')
                self.assertIsNone(worker.once())
                self.queue.db.execute('UPDATE jobs SET available=0')
        self.assertEqual(self.queue.status(run)[0]['state'], 'failed')
        self.assertEqual(self.queue.status(run)[0]['attempts'], 2)
        self.assertIsNone(worker.once())

    def test_retry_then_success(self):
        run = self.submit()
        worker = Worker(self.queue)
        worker.once(simulation='rate_limited')
        self.queue.db.execute('UPDATE jobs SET available=0')
        worker.once()
        self.assertEqual(self.queue.status(run)[0]['state'], 'done')
        self.assertEqual(self.queue.status(run)[0]['attempts'], 2)

    def test_rejected_is_terminal(self):
        run = self.submit()
        Worker(self.queue).once(simulation='rejected')
        self.assertEqual(self.queue.status(run)[0]['state'], 'failed')
        self.assertIsNone(self.queue.claim())

    def test_effective_timeout_kills_hanging_process(self):
        with tempfile.TemporaryDirectory() as folder:
            q = Queue(folder, replace(self.settings, timeout_seconds=0.4))
            try:
                run = q.submit('hang', 'Teste', ('coordenador_bi',))
                before = {p.pid for p in multiprocessing.active_children()}
                start = time.monotonic()
                self.assertEqual(Worker(q).once(simulation='hang')['outcome'], 'timeout')
                self.assertLess(time.monotonic() - start, 2.5)
                self.assertEqual(q.status(run)[0]['state'], 'uncertain')
                self.assertTrue(q.budget()['held_microusd'] > 0)
                self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
                self.assertIsNone(Worker(q).once())
            finally:
                q.close()

    def test_process_crash_no_automatic_replay(self):
        run = self.submit()
        Worker(self.queue).once(simulation='crash')
        self.assertEqual(self.queue.status(run)[0]['state'], 'uncertain')
        self.assertIsNone(Worker(self.queue).once())

    def test_pending_and_completed_survive_reopen(self):
        run = self.submit(agents=('coordenador_bi', 'analytics_finops'))
        Worker(self.queue).once()
        self.queue.close()
        self.queue = Queue(self.temp.name, self.settings)
        Worker(self.queue).drain()
        self.assertTrue(all(j['state'] == 'done' and j['attempts'] == 1 for j in self.queue.status(run)))

    def test_expired_lease_is_uncertain_not_resent(self):
        run = self.submit()
        job = self.queue.claim()
        held = self.queue.budget()['held_microusd']
        self.assertEqual(self.queue.recover(), 0)
        self.queue.db.execute('UPDATE jobs SET lease=0')
        self.assertEqual(self.queue.recover(), 1)
        self.assertEqual(self.queue.status(run)[0]['state'], 'uncertain')
        self.assertEqual(self.queue.budget()['held_microusd'], held)
        self.assertIsNone(self.queue.claim())
        self.assertFalse(self.queue.finish(job, {'code': 'unknown'}))

    def test_stable_ids_and_idempotent_replay(self):
        run = self.submit()
        Worker(self.queue).once()
        statuses = self.queue.status(run)
        self.assertEqual(self.submit(), run)
        self.assertEqual(self.queue.status(run), statuses)
        self.assertIsNone(Worker(self.queue).once())
        with self.assertRaises(ValueError):
            self.submit(text='Diferente')

    def test_concurrent_claims_only_one_owner(self):
        self.submit()
        def claim(_):
            q = Queue(self.temp.name, self.settings)
            try:
                return q.claim()
            finally:
                q.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM consumption').fetchone()[0], 1)

    def test_cancel_queued_never_dispatches(self):
        run = self.submit(agents=('coordenador_bi', 'analytics_finops'))
        self.queue.cancel(run)
        self.assertTrue(all(s['state'] == 'cancelled' for s in self.queue.status(run)))
        self.assertIsNone(Worker(self.queue).once())

    def test_cancel_running_stops_child(self):
        run = self.submit()
        errors = []
        def cancel_when_running():
            q = Queue(self.temp.name, self.settings)
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if q.status(run)[0]['state'] == 'running':
                        q.cancel(run)
                        return
                    time.sleep(0.01)
                errors.append('NÃ£o iniciou')
            finally:
                q.close()
        thread = threading.Thread(target=cancel_when_running)
        thread.start()
        start = time.monotonic()
        Worker(self.queue).once(simulation='hang')
        thread.join(3)
        self.assertFalse(errors)
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertEqual(self.queue.status(run)[0]['state'], 'cancelled_unknown')

    def test_credentials_never_persisted_or_logged(self):
        key = 'sk-FAKE-CANARY-NEVER-REAL'
        private_text = 'SENSITIVE-CONTENT-CANARY'
        with patch.dict(os.environ, {'OPENAI_API_KEY': key}):
            run = self.submit(text=private_text)
            with patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
                Worker(self.queue).once(simulation='exception')
            self.assertNotIn(key, out.getvalue() + err.getvalue())
            self.assertNotIn(key, repr(self.settings))
        operations = (Path(self.temp.name) / 'operations.sqlite3').read_bytes()
        private = (Path(self.temp.name) / 'private.sqlite3').read_bytes()
        self.assertNotIn(private_text.encode(), operations)
        self.assertIn(private_text.encode(), private)
        self.assertNotIn(key.encode(), operations + private)
        self.assertEqual(self.queue.status(run)[0]['state'], 'uncertain')

    def test_live_gate_requires_all_switches_before_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = replace(self.settings, mode='live')
            q = Queue(folder, settings)
            try:
                run = q.submit('live', 'Teste', ('coordenador_bi',))
                with patch.dict(os.environ, {'P3_ENABLE_OPENAI': 'false', 'OPENAI_API_KEY': 'fake'}):
                    with self.assertRaises(PermissionError):
                        Worker(q, allow_paid=True).once()
                self.assertEqual(q.status(run)[0]['attempts'], 0)
            finally:
                q.close()

    def test_live_credentials_from_env_and_fixed_endpoint(self):
        settings = replace(self.settings, mode='live')
        response = MagicMock(status=200)
        response.read.return_value = json.dumps({'status':'completed', 'usage': {'input_tokens': 100, 'output_tokens': 16},
            'output':[{'type':'message','content':[{'type':'output_text','text':'Resposta simulada'}]}]}).encode()
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.dict(os.environ, {'P3_ENABLE_OPENAI': 'true', 'P3_PRICES_ACK': 'true', 'OPENAI_API_KEY':'fake-key'}), \
             patch('http.client.HTTPSConnection', return_value=connection) as constructor:
            result = openai_request(settings, {'id':'stable-id', 'agent':'coordenador_bi','text':'Teste'}, True)
        self.assertEqual(result['code'], 'ok')
        self.assertEqual(constructor.call_args.args, ('api.openai.com',))
        request = connection.request.call_args
        self.assertEqual(request.args[:2], ('POST', '/v1/responses'))
        self.assertEqual(request.kwargs['headers']['X-Client-Request-Id'], 'stable-id')
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer fake-key')
        body = json.loads(request.kwargs['body'])
        self.assertEqual(body['max_output_tokens'], settings.max_output_tokens)
        self.assertEqual(body['tools'], [])
        self.assertFalse(body['store'])
        self.assertEqual(body['reasoning']['effort'], 'none')

    def test_direct_transport_cannot_bypass_mode(self):
        with self.assertRaises(PermissionError):
            openai_request(self.settings, {}, True)
        with patch.dict(os.environ, {'P3_ENABLE_OPENAI':'true','P3_PRICES_ACK':'true','OPENAI_API_KEY':'fake\nheader'}):
            with self.assertRaises(PermissionError):
                live_ready(replace(self.settings, mode='live'), True)

    def test_approval_bound_to_immutable_content(self):
        action = self.action()
        view = self.queue.review_action(action)
        self.assertEqual(view['state'], 'awaiting_approval')
        with self.assertRaises(PermissionError):
            self.queue.decide(action, 'tampered', approve=True, reviewer='human')
        self.queue.decide(action, view['fingerprint'], approve=True, reviewer='human')
        self.assertEqual(self.queue.review_action(action)['state'], 'approved')
        with self.assertRaises(PermissionError):
            self.queue.execute_action(action)
        with self.assertRaises(PermissionError):
            self.queue.decide(action, view['fingerprint'], approve=True, reviewer='human')

    def test_action_rejection_and_revocation(self):
        action = self.action()
        view = self.queue.review_action(action)
        self.queue.decide(action, view['fingerprint'], approve=False, reviewer='human')
        self.assertEqual(self.queue.review_action(action)['state'], 'rejected')
        with self.assertRaises(PermissionError):
            self.queue.execute_action(action)

    def test_expired_approval_denied(self):
        action = self.action()
        self.queue.db.execute('UPDATE actions SET expires=0')
        view = self.queue.review_action(action)
        with self.assertRaises(PermissionError):
            self.queue.decide(action, view['fingerprint'], approve=True, reviewer='human')

    def test_cancel_revokes_approval_and_blocks_new_actions(self):
        run, job = self.completed()
        action = self.queue.propose_action('a', job, 'write_file', {'path': 'example.txt'})
        self.queue.cancel(run)
        self.assertEqual(self.queue.review_action(action)['state'], 'revoked')
        with self.assertRaises(PermissionError):
            self.queue.propose_action('b', job, 'commitment', {'text': 'Confirmar'})

    def test_explicit_revoke(self):
        action = self.action()
        view = self.queue.review_action(action)
        self.queue.decide(action, view['fingerprint'], approve=True, reviewer='human')
        self.queue.revoke(action)
        self.assertEqual(self.queue.review_action(action)['state'], 'revoked')

    def test_action_idempotency_and_changed_content(self):
        run, job = self.completed()
        action = self.queue.propose_action('action', job, 'commitment', {'text':'a'})
        self.assertEqual(self.queue.propose_action('action', job, 'commitment', {'text':'a'}), action)
        with self.assertRaises(ValueError):
            self.queue.propose_action('action', job, 'commitment', {'text':'changed'})

    def test_pending_job_cannot_propose_action(self):
        run = self.submit()
        with self.assertRaises(PermissionError):
            self.queue.propose_action('a', self.queue.status(run)[0]['id'], 'send_email', {})

    def test_limits_and_cycle_rejection(self):
        for agents in ((), ('coordenador_bi','coordenador_bi'), ('grok',)):
            with self.assertRaises(ValueError):
                self.submit(agents=agents)
        with self.assertRaises(ValueError):
            self.submit(text='x'*24001)
        with self.assertRaises(ValueError):
            Worker(self.queue).drain(max_jobs=19)
        for values in ({'max_attempts':4},{'model':'gemini'},{'timeout_seconds':float('nan')},{'budget_microusd':-1}):
            with self.assertRaises(ValueError):
                replace(self.settings, **values)

    def test_estimate_includes_instruction_and_output(self):
        result = estimate(self.settings, 'analytics_finops', 'Ã§'*20)
        self.assertGreater(result['reserved_input_tokens'], result['estimated_input_tokens'])
        self.assertGreater(result['reserve_microusd'], result['estimated_microusd'])
        self.assertEqual(cost_microusd(self.settings.model, 1000, 256), 1902)

    def test_policy_cannot_change_on_reopen(self):
        with self.assertRaises(ValueError):
            Queue(self.temp.name, replace(self.settings, mode='live'))

    def test_incomplete_database_pair_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'operations.sqlite3').touch()
            with self.assertRaises(ValueError):
                Queue(folder, self.settings)

    def test_provider_responses_do_not_become_tools(self):
        bad = {'status':'completed','usage':{'input_tokens':1,'output_tokens':1},
               'output':[{'type':'function_call','name':'send_email','arguments':'{}'}]}
        self.assertEqual(parse_response(bad), {'code':'invalid'})
        self.assertEqual(parse_response({'status':'incomplete'}), {'code':'invalid'})
        self.assertEqual(validate_outcome({'code':'ok','text':'x','input_tokens':True,'output_tokens':1}), {'code':'invalid'})
        self.assertEqual(validate_outcome({'code':'unknown','error':'secret'}), {'code':'unknown'})

    def test_invalid_env_values_not_in_exception(self):
        with patch.dict(os.environ, {'P3_BUDGET_MICROUSD':'PRIVATE-CANARY'}):
            with self.assertRaises(ValueError) as error:
                Settings.from_env()
        self.assertNotIn('PRIVATE-CANARY', str(error.exception))

    def test_all_live_gates_and_missing_key(self):
        settings = replace(self.settings, mode='live')
        for allow, enable, ack, key in ((False,'true','true','fake'), (True,'false','true','fake'),
                                       (True,'true','false','fake'), (True,'true','true','')):
            with patch.dict(os.environ, {'P3_ENABLE_OPENAI':enable,'P3_PRICES_ACK':ack,'OPENAI_API_KEY':key}):
                with self.assertRaises(PermissionError):
                    live_ready(settings, allow)

    def test_http_status_classification_without_hidden_retries(self):
        settings = replace(self.settings, mode='live')
        for status, code in ((429,'rate_limited'),(401,'rejected'),(500,'unknown'),(408,'unknown')):
            connection = MagicMock()
            connection.getresponse.return_value.status = status
            with patch.dict(os.environ, {'P3_ENABLE_OPENAI':'true','P3_PRICES_ACK':'true','OPENAI_API_KEY':'fake'}), \
                 patch('http.client.HTTPSConnection', return_value=connection):
                result = openai_request(settings, {'id':'id','agent':'coordenador_bi','text':'text'}, True)
            self.assertEqual(result, {'code':code})
            self.assertEqual(connection.request.call_count, 1)
            connection.getresponse.return_value.read.assert_not_called()

    def test_approval_detects_private_payload_tampering(self):
        action = self.action()
        fingerprint = self.queue.review_action(action)['fingerprint']
        self.queue.db.execute('UPDATE private.action_content SET payload=? WHERE id=?', ('{"body":"alterado"}', action))
        with self.assertRaises(PermissionError):
            self.queue.decide(action, fingerprint, approve=True, reviewer='human')

    def test_retry_persistence_and_budget_shared_after_restart(self):
        run = self.submit()
        Worker(self.queue).once(simulation='rate_limited')
        held = self.queue.budget()['held_microusd']
        self.queue.close()
        self.queue = Queue(self.temp.name, self.settings)
        self.assertEqual(self.queue.budget()['held_microusd'], held)
        self.queue.db.execute('UPDATE jobs SET available=0')
        Worker(self.queue).once()
        self.assertEqual(self.queue.status(run)[0]['attempts'], 2)
        self.assertEqual(self.queue.budget()['held_microusd'], held * 2)

    def test_private_handoff_input_limit_blocks_next_step(self):
        run = self.submit(agents=('coordenador_bi','analista_bi'))
        job = self.queue.claim()
        self.queue.finish(job, {'code':'ok','text':'x'*12000,'input_tokens':1,'output_tokens':1})
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.status(run)[1]['state'], 'failed')

    def test_queue_transaction_rolls_back_interrupted_completion(self):
        run = self.submit(agents=('coordenador_bi','analista_bi'))
        job = self.queue.claim()
        with patch.object(self.queue, 'event', side_effect=RuntimeError('Falha de gravaÃ§Ã£o simulada')):
            with self.assertRaises(RuntimeError):
                self.queue.finish(job, {'code':'ok','text':'Resposta','input_tokens':1,'output_tokens':1})
        self.assertEqual(self.queue.status(run)[0]['state'], 'running')
        self.assertIsNone(self.queue.private_messages(run)[0]['output'])
        self.assertEqual(self.queue.status(run)[1]['state'], 'waiting')

    def test_cli_proposal_and_human_confirmation(self):
        from central.phase3.__main__ import main
        run, job = self.completed()
        payload = Path(self.temp.name) / 'action.json'
        payload.write_text('{"body":"Rascunho ficticio"}', encoding='utf-8')
        base = ['phase3', '--directory', self.temp.name]
        with patch('central.phase3.__main__.Settings.from_env', return_value=self.settings):
            with patch('sys.argv', base + ['propose-action','--key','cli-action','--job',job,
                        '--kind','send_email','--payload-file',str(payload)]), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                main()
            action = output.getvalue().strip()
            view = self.queue.review_action(action)
            with patch('sys.argv', base + ['approve','--action',action,'--fingerprint',view['fingerprint'],
                        '--reviewer','local-human','--human-confirm']):
                main()
        self.assertEqual(self.queue.review_action(action)['state'], 'approved')
        with self.assertRaises(PermissionError):
            self.queue.execute_action(action)
