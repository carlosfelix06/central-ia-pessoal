import json
import multiprocessing
import time
from .transport import child_entry, live_ready


def validate_outcome(value):
    if not isinstance(value, dict) or value.get('code') not in (
            'ok', 'rate_limited', 'rejected', 'invalid', 'unknown'):
        return {'code': 'invalid'}
    if value['code'] != 'ok':
        return {'code': value['code']}
    if (not isinstance(value.get('text'), str) or not value['text'].strip()
            or len(value['text'].encode()) > 32768
            or any(type(value.get(k)) is not int or not 0 <= value[k] <= 2000000
                   for k in ('input_tokens', 'output_tokens'))):
        return {'code': 'invalid'}
    return {k: value[k] for k in ('code', 'text', 'input_tokens', 'output_tokens')}


class Worker:
    def __init__(self, queue, *, allow_paid=False):
        self.queue = queue
        self.allow_paid = allow_paid

    def once(self, *, simulation='success'):
        settings = self.queue.settings
        live_ready(settings, self.allow_paid)  # Antes de reservar ou retirar da fila.
        job = self.queue.claim()
        if not job:
            return None
        return supervise(self.queue, job, settings, self.allow_paid, simulation)

    def drain(self, *, max_jobs=6):
        if type(max_jobs) is not int or not 1 <= max_jobs <= 18:
            raise ValueError('MÃ¡ximo de 18 execuÃ§Ãµes por invocaÃ§Ã£o')
        results = []
        for _ in range(max_jobs):
            result = self.once()
            if result is None:
                break
            results.append(result)
        return results


def supervise(queue, job, settings, allow_paid, simulation, entry=child_entry):
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=entry,
        args=(sender, settings, job, allow_paid, simulation), daemon=True)
    outcome = {'code': 'unknown'}
    started = time.monotonic()
    try:
        if queue.is_cancelled(job):
            outcome = {'code': 'cancelled_unknown'}
        else:
            process.start()
            sender.close()
            while True:
                if queue.is_cancelled(job):
                    outcome = {'code': 'cancelled_unknown'}
                    break
                remaining = settings.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    outcome = {'code': 'timeout'}
                    break
                if receiver.poll(min(0.02, remaining)):
                    try:
                        outcome = validate_outcome(json.loads(receiver.recv_bytes(131072)))
                    except (EOFError, OSError, ValueError, TypeError):
                        outcome = {'code': 'unknown'}
                    break
                if not process.is_alive():
                    break
    except Exception:
        outcome = {'code': 'unknown'}
    except BaseException:
        queue.finish(job, {'code': 'unknown'})
        raise
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            process.close()
        receiver.close()
        sender.close()
    queue.finish(job, outcome)
    return {'job': job['id'], 'outcome': outcome['code']}
