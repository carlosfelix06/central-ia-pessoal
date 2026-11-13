from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

from central.phase3.policy import Settings, AGENTS
from central.phase3.queue import Queue
from central.integrations.policy import Route, GEMINI_MODEL, cost, estimate_route
from central.integrations.queue import RoutedQueue
from central.integrations.worker import RoutedWorker
from central.integrations.transport import build_client, gemini_request, parse_gemini, preflight
from central.integrations.credentials import gemini_key, inspect_credentials, load_credentials
from tools.security_check import inspect_file


def response(text='Resposta fictícia',thinking=0):
    return NS(candidates=[NS(finish_reason='STOP',content=NS(parts=[NS(text=text,thought=False,function_call=None)]))],
              usage_metadata=NS(prompt_token_count=100,candidates_token_count=16,thoughts_token_count=thinking,
                                total_token_count=116+thinking))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.settings=Settings(budget_microusd=100000,timeout_seconds=10)
        self.rpm={'openai':60,'gemini':60}
        self.queue=RoutedQueue(self.folder.name,self.settings,rpm=self.rpm)

    def tearDown(self):
        self.queue.close()
        self.folder.cleanup()

    def submit(self,key='one',agents=('coordenador_bi',),routes=None):
        return self.queue.submit(key,'Pedido fictício sem dados privados',agents,routes=routes or [Route('gemini') for _ in agents])

    def test_six_specialists_two_explicit_providers_offline(self):
        agents=tuple(AGENTS)
        routes=[Route('openai' if i%2==0 else 'gemini',allow_cross_provider=i>0) for i in range(6)]
        run=self.submit(agents=agents,routes=routes)
        with patch('http.client.HTTPSConnection',side_effect=AssertionError('No API')):
            RoutedWorker(self.queue).drain()
        states=self.queue.route_status(run)
        self.assertTrue(all(s['state']=='done' for s in states))
        self.assertEqual([s['provider'] for s in states],[r.provider for r in routes])
        messages=self.queue.private_messages(run)
        for prev,current in zip(messages,messages[1:]):
            self.assertEqual(json.loads(current['input'])['resultado_anterior'],prev['output'])

    def test_cross_provider_requires_specific_consent(self):
        with self.assertRaises(PermissionError):
            self.submit(agents=('coordenador_bi','analytics_finops'),routes=[Route('openai'),Route('gemini')])
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_idempotency_includes_provider_model_and_permissions(self):
        run=self.submit()
        self.assertEqual(self.submit(),run)
        with self.assertRaises(ValueError):
            self.submit(routes=[Route('openai')])
        self.assertEqual(self.queue.db.execute('SELECT COUNT(*) FROM task_routes').fetchone()[0],1)

    def test_explicit_route_required(self):
        with self.assertRaises(ValueError):
            self.queue.submit('no-route','Texto')

    def test_unknown_provider_and_model_denied(self):
        for kwargs in ({'provider':'other'},{'provider':'gemini','model':'obsolete'},{'provider':'gemini','max_output_tokens':0}):
            with self.assertRaises(ValueError):
                Route(**kwargs)

    def test_gemini_pricing_and_estimate(self):
        route=Route('gemini')
        self.assertEqual(route.model,GEMINI_MODEL)
        self.assertEqual(cost(route,1000,256),940)
        result=estimate_route(self.settings,route,'coordenador_bi','Oi')
        self.assertGreater(result['reserve_microusd'],result['estimated_microusd'])

    def test_provider_budget_is_independent_but_global_also_applies(self):
        with tempfile.TemporaryDirectory() as folder:
            q=RoutedQueue(folder,self.settings,provider_budgets={'gemini':0,'openai':100000})
            try:
                run=q.submit('x','Texto',('coordenador_bi',),routes=[Route('gemini')])
                self.assertIsNone(RoutedWorker(q).once())
                self.assertEqual(q.status(run)[0]['state'],'budget_blocked')
                self.assertEqual(q.budget()['held_microusd'],0)
            finally:q.close()

    def test_shared_global_budget_blocks_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            q=RoutedQueue(folder,replace(self.settings,budget_microusd=0))
            try:
                run=q.submit('x','Texto',('coordenador_bi',),routes=[Route('gemini')])
                self.assertIsNone(RoutedWorker(q).once())
                self.assertEqual(q.status(run)[0]['attempts'],0)
            finally:q.close()

    def test_rate_limit_survives_reopen_and_does_not_reserve_early(self):
        with tempfile.TemporaryDirectory() as folder:
            q=RoutedQueue(folder,self.settings)
            runs=[q.submit(str(i),'Texto',('coordenador_bi',),routes=[Route('gemini')]) for i in range(3)]
            RoutedWorker(q).drain()
            self.assertEqual(q.status(runs[-1])[0]['attempts'],0)
            self.assertEqual(q.db.execute('SELECT COUNT(*) FROM route_attempts').fetchone()[0],2)
            q.close()
            q=RoutedQueue(folder,self.settings)
            try:
                self.assertIsNone(RoutedWorker(q).once())
                q.db.execute('UPDATE route_attempts SET at=0')
                q.db.execute('UPDATE jobs SET available=0')
                RoutedWorker(q).once()
                self.assertEqual(q.status(runs[-1])[0]['state'],'done')
            finally:q.close()

    def test_resume_preserves_routes_and_confirmed_handoffs(self):
        run=self.submit(agents=('coordenador_bi','analytics_finops'))
        RoutedWorker(self.queue).once()
        self.queue.close()
        self.queue=RoutedQueue(self.folder.name,self.settings,rpm=self.rpm)
        RoutedWorker(self.queue).drain()
        self.assertTrue(all(s['state']=='done' and s['attempts']==1 for s in self.queue.route_status(run)))

    def test_rate_failure_retries_only_same_route(self):
        run=self.submit()
        worker=RoutedWorker(self.queue)
        worker.once(simulation='rate_limited')
        self.queue.db.execute('UPDATE jobs SET available=0')
        worker.once()
        self.assertEqual(self.queue.route_status(run)[0]['attempts'],2)
        self.assertEqual(self.queue.route_status(run)[0]['provider'],'gemini')

    def test_ambiguous_failure_has_no_fallback_or_downstream_dispatch(self):
        run=self.submit(agents=('coordenador_bi','analytics_finops'),routes=[Route('gemini'),Route('openai',allow_cross_provider=True)])
        worker=RoutedWorker(self.queue)
        worker.once(simulation='unknown')
        self.assertIsNone(worker.once())
        rows=self.queue.route_status(run)
        self.assertEqual(rows[0]['state'],'uncertain')
        self.assertEqual(rows[1]['attempts'],0)
        self.assertGreater(self.queue.budget()['held_microusd'],0)

    def test_inherited_approval_gate_remains_disabled(self):
        run=self.submit()
        RoutedWorker(self.queue).once()
        action=self.queue.propose_action('action',self.queue.status(run)[0]['id'],'send_email',{'text':'fictício'})
        view=self.queue.review_action(action)
        self.queue.decide(action,view['fingerprint'],approve=True,reviewer='human')
        with self.assertRaises(PermissionError):
            self.queue.execute_action(action)

    def test_inherited_cancel_and_recovery_do_not_change_route(self):
        run=self.submit()
        job=self.queue.claim()
        self.queue.db.execute('UPDATE jobs SET lease=0')
        self.queue.recover()
        self.assertEqual(self.queue.route_status(run)[0]['state'],'uncertain')
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.route(job).provider,'gemini')
        run2=self.submit('two')
        self.queue.cancel(run2)
        self.assertEqual(self.queue.status(run2)[0]['state'],'cancelled')

    def test_policy_isolation_from_legacy_queue(self):
        with self.assertRaises(ValueError):
            Queue(self.folder.name,self.settings)
        with self.assertRaises(ValueError):
            RoutedQueue(self.folder.name,self.settings,rpm={'openai':2,'gemini':2})

    def test_route_missing_fails_closed(self):
        self.submit()
        self.queue.db.execute('DELETE FROM task_routes')
        with self.assertRaises(PermissionError):
            self.queue.claim()

    def test_live_gate_blocks_before_any_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            q=RoutedQueue(folder,replace(self.settings,mode='live'))
            try:
                run=q.submit('live','Texto',('coordenador_bi',),routes=[Route('gemini')])
                with patch.dict(os.environ,{'INTEGRATIONS_ENABLE_REAL':'false'}):
                    with self.assertRaises(PermissionError):
                        RoutedWorker(q).once()
                self.assertEqual(q.status(run)[0]['attempts'],0)
            finally:q.close()

    def test_direct_transport_cannot_bypass_mode_or_authorization(self):
        with self.assertRaises(PermissionError):
            gemini_request(self.settings,Route('gemini'),{},True)
        with patch.dict(os.environ,{'INTEGRATIONS_ENABLE_REAL':'true','INTEGRATIONS_PRICES_ACK':'true','GEMINI_API_KEY':'fake'},clear=True):
            with self.assertRaises(PermissionError):
                preflight(replace(self.settings,mode='live'),Route('gemini'),False)

    def test_sdk_configuration_uses_fixed_host_timeout_and_single_attempt(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':'synthetic'},clear=True),patch('google.genai.Client') as factory:
            build_client(self.settings)
        kwargs=factory.call_args.kwargs
        self.assertFalse(kwargs['vertexai'])
        self.assertEqual(kwargs['http_options'].base_url,'https://generativelanguage.googleapis.com')
        self.assertEqual(kwargs['http_options'].timeout,10000)
        self.assertEqual(kwargs['http_options'].retry_options.attempts,1)

    def test_sdk_request_is_mocked_and_bounded(self):
        client=MagicMock()
        client.__enter__.return_value=client
        client.models.generate_content.return_value=response()
        settings=replace(self.settings,mode='live')
        with patch.dict(os.environ,{'INTEGRATIONS_ENABLE_REAL':'true','INTEGRATIONS_PRICES_ACK':'true','GEMINI_API_KEY':'fake'},clear=True), \
             patch('central.integrations.transport.build_client',return_value=client):
            result=gemini_request(settings,Route('gemini'),{'agent':'coordenador_bi','text':'synthetic'},True)
        self.assertEqual(result['code'],'ok')
        call=client.models.generate_content.call_args.kwargs
        self.assertEqual(call['model'],GEMINI_MODEL)
        self.assertEqual(call['config'].max_output_tokens,256)
        self.assertTrue(call['config'].automatic_function_calling.disable)
        self.assertEqual(call['config'].tools,[])
        self.assertEqual(client.models.generate_content.call_count,1)

    def test_thinking_usage_accounted_and_unsafe_output_rejected(self):
        self.assertEqual(parse_gemini(response(thinking=20))['output_tokens'],36)
        invalid=response();invalid.candidates[0].finish_reason='MAX_TOKENS'
        self.assertEqual(parse_gemini(invalid)['code'],'invalid')
        invalid=response();invalid.candidates[0].content.parts[0].function_call=NS(name='send_email')
        self.assertEqual(parse_gemini(invalid)['code'],'invalid')

    def test_sdk_errors_are_sanitized_and_classified(self):
        client=MagicMock();client.__enter__.return_value=client
        class APIError(Exception):
            def __init__(self,code):self.code=code
        for status,expected in ((429,'rate_limited'),(403,'rejected'),(503,'unknown')):
            client.models.generate_content.side_effect=APIError(status)
            with patch.dict(os.environ,{'INTEGRATIONS_ENABLE_REAL':'true','INTEGRATIONS_PRICES_ACK':'true','GEMINI_API_KEY':'fake'},clear=True), \
                 patch('central.integrations.transport.build_client',return_value=client):
                result=gemini_request(replace(self.settings,mode='live'),Route('gemini'),{'agent':'coordenador_bi','text':'data'},True)
            self.assertEqual(result,{'code':expected})

    def test_env_inspection_returns_presence_only(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{},clear=True):
            file=Path(folder)/'.env';file.write_text('GEMINI_API_KEY=hidden-canary\nINTEGRATIONS_ENABLE_REAL=true\n',encoding='utf-8')
            status=inspect_credentials(file)
            self.assertTrue(status['keys']['GEMINI_API_KEY']['in_file'])
            self.assertNotIn('hidden-canary',json.dumps(status))
            self.assertNotIn('GEMINI_API_KEY',os.environ)
            load_credentials(file)
            self.assertEqual(gemini_key(),'hidden-canary')
            self.assertNotIn('INTEGRATIONS_ENABLE_REAL',os.environ)

    def test_env_sources_conflict_and_aliases_do_not_silently_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'.env';file.write_text('GEMINI_API_KEY=from-file',encoding='utf-8')
            with patch.dict(os.environ,{'GEMINI_API_KEY':'from-process'},clear=True):
                with self.assertRaises(PermissionError):load_credentials(file)
                self.assertEqual(os.environ['GEMINI_API_KEY'],'from-process')
        with patch.dict(os.environ,{'GEMINI_API_KEY':'one','GOOGLE_API_KEY':'two'},clear=True):
            with self.assertRaises(PermissionError):gemini_key()

    def test_interpolation_is_not_used_for_credentials(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'PRIVATE':'opaque'},clear=True):
            file=Path(folder)/'.env';file.write_text('GEMINI_API_KEY=${PRIVATE}',encoding='utf-8')
            load_credentials(file)
            self.assertEqual(os.environ['GEMINI_API_KEY'],'${PRIVATE}')

    def test_audit_does_not_contain_private_payloads_or_credentials(self):
        run=self.queue.submit('sensitive','PRIVATE-PAYLOAD-CANARY',('coordenador_bi',),routes=[Route('gemini')])
        RoutedWorker(self.queue).once()
        operational=Path(self.folder.name,'operations.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE-PAYLOAD-CANARY',operational)
        self.assertEqual(self.queue.route_status(run)[0]['provider'],'gemini')

    def test_google_secret_patterns_are_blocked(self):
        self.assertEqual(inspect_file('example.py',('AIza'+'x'*35).encode()),'secret_pattern')

    def test_routed_timeout_stops_child_and_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as folder:
            q=RoutedQueue(folder,replace(self.settings,timeout_seconds=0.4))
            try:
                run=q.submit('hang','Texto',('coordenador_bi',),routes=[Route('gemini')])
                started=time.monotonic()
                self.assertEqual(RoutedWorker(q).once(simulation='hang')['outcome'],'timeout')
                self.assertLess(time.monotonic()-started,5)
                self.assertEqual(q.status(run)[0]['state'],'uncertain')
                self.assertIsNone(RoutedWorker(q).once())
                self.assertGreater(q.budget()['held_microusd'],0)
            finally:q.close()

    def test_routed_retry_attempts_are_bounded(self):
        run=self.submit()
        worker=RoutedWorker(self.queue)
        worker.once(simulation='rate_limited')
        self.queue.db.execute('UPDATE jobs SET available=0')
        worker.once(simulation='rate_limited')
        self.assertIsNone(worker.once())
        self.assertEqual(self.queue.status(run)[0]['state'],'failed')
        self.assertEqual(self.queue.status(run)[0]['attempts'],2)

    def test_staged_secret_is_blocked_even_when_working_copy_is_clean(self):
        from tools import security_check
        from contextlib import redirect_stdout
        with patch.object(security_check,'candidates',return_value=[]), \
             patch.object(security_check,'staged_files',return_value=[('safe.py',('AIza'+'x'*35).encode())]), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):security_check.main()
        self.assertIn('index_secret_pattern',output.getvalue())
        self.assertNotIn('x'*35,output.getvalue())

    def test_private_integration_queues_cannot_be_published(self):
        for name in ('integrations-live/file.txt','integrations-demo/report.json','integrations-local/policy.txt'):
            self.assertEqual(inspect_file(name,b''),'private_artifact')

    def test_event_filters_cover_routing_and_security_configuration(self):
        from tools.coordinate_copilot import relevant
        for path in ('config/integrations-example.json','config/copilot-approved-tools.json',
                     'scripts/Test-Automation.ps1','.env.example','.gitignore'):
            self.assertTrue(relevant([path]))
        self.assertFalse(relevant(['docs/INTEGRACOES-PROVEDORES.md','README.md']))
