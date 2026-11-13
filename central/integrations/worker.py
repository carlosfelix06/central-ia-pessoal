from dataclasses import asdict
from central.phase3.worker import Worker, supervise
from .transport import child_entry, preflight


class RoutedWorker(Worker):
    def __init__(self,queue,*,allow_real=False):
        super().__init__(queue,allow_paid=allow_real)
        self.allow_real=allow_real

    def once(self,*,simulation='success'):
        previous=self.queue.authorizer
        self.queue.authorizer=lambda route:preflight(self.queue.settings,route,self.allow_real)
        try:
            job=self.queue.claim()
        finally:
            self.queue.authorizer=previous
        if job is None:
            return None
        job['route']=asdict(self.queue.route(job))
        return supervise(self.queue,job,self.queue.settings,self.allow_real,simulation,entry=child_entry)
