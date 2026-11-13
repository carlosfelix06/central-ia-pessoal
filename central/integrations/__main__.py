import argparse
from dataclasses import asdict
import json
from pathlib import Path
from central.phase3.policy import Settings, AGENTS
from .credentials import inspect_credentials, load_credentials
from .policy import Route
from .queue import RoutedQueue
from .worker import RoutedWorker


def configuration(path):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    settings=Settings(**data['settings'])
    steps=data['steps']
    agents=[step['agent'] for step in steps]
    routes=[Route(**{k:v for k,v in step.items() if k!='agent'}) for step in steps]
    return settings,data.get('provider_budgets'),data.get('rpm'),agents,routes


def demo(directory):
    settings=Settings(budget_microusd=100000,timeout_seconds=10)
    agents=list(AGENTS)
    routes=[Route('openai' if i%2==0 else 'gemini',allow_cross_provider=i>0) for i in range(6)]
    queue=RoutedQueue(directory,settings,rpm={'openai':60,'gemini':60})
    try:
        run=queue.submit('two-providers-six-agents-v1','Cenário analítico fictício de BI e Data Warehouse na Matriz-SP.',agents,routes=routes)
        RoutedWorker(queue).drain()
        report={'mode':'mock','api_calls':0,'automatic_fallback':False,'run':run,
                'tasks':queue.route_status(run),'messages':queue.private_messages(run),'budget':queue.budget()}
        Path(directory,'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'mode':'mock','api_calls':0,'done':sum(t['state']=='done' for t in report['tasks']),
                          'report':str(Path(directory,'report.json'))}))
    finally:
        queue.close()


def main():
    parser=argparse.ArgumentParser(description='Rotas explícitas OpenAI/Gemini; mock por padrão')
    parser.add_argument('--directory',default='integrations-local')
    parser.add_argument('--config',default='config/integrations-example.json')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('demo')
    inspect=commands.add_parser('inspect-credentials')
    inspect.add_argument('--env-file',default='.env')
    submit=commands.add_parser('submit')
    submit.add_argument('--key',required=True)
    submit.add_argument('--input-file',required=True)
    work=commands.add_parser('work')
    work.add_argument('--max-jobs',type=int,default=1)
    work.add_argument('--allow-real',action='store_true')
    work.add_argument('--env-file')
    for name in ('status','cancel'):
        commands.add_parser(name).add_argument('--run',required=True)
    args=parser.parse_args()
    if args.command=='inspect-credentials':
        print(json.dumps(inspect_credentials(args.env_file)))
        return
    if args.command=='demo':
        demo(args.directory)
        return
    settings,budgets,rpm,agents,routes=configuration(args.config)
    queue=RoutedQueue(args.directory,settings,provider_budgets=budgets,rpm=rpm)
    try:
        if args.command=='submit':
            print(queue.submit(args.key,Path(args.input_file).read_text(encoding='utf-8'),agents,routes=routes))
        elif args.command=='work':
            if settings.mode=='live' and args.allow_real and args.env_file:
                load_credentials(args.env_file)
            print(json.dumps(RoutedWorker(queue,allow_real=args.allow_real).drain(max_jobs=args.max_jobs)))
        elif args.command=='status':
            print(json.dumps(queue.route_status(args.run)))
        elif args.command=='cancel':
            queue.cancel(args.run)
    finally:
        queue.close()


if __name__=='__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Integração interrompida com segurança; valores privados omitidos. Confira configuração e estados.') from None
