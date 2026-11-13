"""Orquestrador da Ponte Central: coordenação entre agentes, filas, handoffs e aprovações."""
from typing import Any, Dict, List, Optional, Sequence
import uuid

from central.core import AGENTS as CORE_AGENTS
from .auth import ApprovalAuthenticator
from .contract import BridgeTask
from .connectors import (
    BaseConnector,
    SimulatedConnector,
    OpenAIConnector,
    GeminiConnector,
    GitHubConnector,
    CopilotConnector,
    AntigravityConnector,
    CodexConnector,
)
from .queue import BridgeQueue


def identify_agent_from_text(text: str) -> str:
    """Classifica o pedido com base no dicionário semântico oficial do central.core."""
    text_lower = text.lower()
    for agent_key, (_, keywords) in CORE_AGENTS.items():
        if any(keyword in text_lower for keyword in keywords):
            return agent_key
    return 'coordenador_bi'


class BridgeOrchestrator:
    """Orquestrador central de mensagens, automações e especialidades."""

    def __init__(
        self,
        queue: Optional[BridgeQueue] = None,
        *,
        db_path: str = 'central.sqlite3',
        authenticator: Optional[ApprovalAuthenticator] = None,
    ) -> None:
        self.authenticator = authenticator or ApprovalAuthenticator()
        self.queue = queue or BridgeQueue(db_path, authenticator=self.authenticator)
        self.connectors: Dict[str, BaseConnector] = {}
        self._register_default_connectors()

    def _register_default_connectors(self) -> None:
        """Registra os conectores padrão para os seis especialistas e ferramentas suportadas."""
        for agent_key, (name, _) in CORE_AGENTS.items():
            self.connectors[agent_key] = SimulatedConnector(name=agent_key)

        self.connectors['openai'] = OpenAIConnector(mode='simulated')
        self.connectors['gemini'] = GeminiConnector(mode='simulated')
        self.connectors['github'] = GitHubConnector(mode='simulated')
        self.connectors['copilot'] = CopilotConnector(mode='simulated')
        self.connectors['antigravity'] = AntigravityConnector()
        self.connectors['codex'] = CodexConnector()

    def register_connector(self, name: str, connector: BaseConnector) -> None:
        """Permite plugar ou substituir executores e conectores específicos."""
        self.connectors[name] = connector

    def register_task(
        self,
        payload: Any,
        *,
        source: str = 'user',
        target: Optional[str] = None,
        chain: Optional[Sequence[str]] = None,
        require_approval: bool = False,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ponto de entrada único: registra uma tarefa apenas uma vez na Central."""
        corr_id = correlation_id or f'corr-{uuid.uuid4().hex[:12]}'
        meta = dict(metadata or {})

        # Converter texto livre em dicionário de entrada padronizado
        if isinstance(payload, str):
            authorized_input = {'raw_text': payload}
            text_for_routing = payload
        elif isinstance(payload, dict):
            authorized_input = dict(payload)
            text_for_routing = str(payload.get('text', payload.get('descricao', '')))
        else:
            raise ValueError('Payload deve ser string ou dicionário')

        # Determinar cadeia e agente inicial
        if chain:
            target_agent = chain[0]
            remaining_chain = list(chain[1:])
        else:
            target_agent = target or identify_agent_from_text(text_for_routing)
            remaining_chain = []

        meta['chain'] = list(chain) if chain else [target_agent]
        meta['remaining_chain'] = remaining_chain

        task = BridgeTask(
            task_id=f'task-{uuid.uuid4().hex[:12]}',
            correlation_id=corr_id,
            source_agent=source,
            target_agent=target_agent,
            authorized_input=authorized_input,
            human_approval_required=require_approval,
            metadata=meta,
        )

        task_id = self.queue.submit(task)
        challenge_info = None

        if require_approval:
            challenge_info = self.queue.request_approval(task_id, 'task_dispatch', authorized_input)

        return {
            'task_id': task_id,
            'correlation_id': corr_id,
            'target_agent': target_agent,
            'state': 'awaiting_approval' if require_approval else 'queued',
            'chain': meta['chain'],
            'approval_id': challenge_info['approval_id'] if challenge_info else None,
            'challenge': challenge_info['challenge'] if challenge_info else None,
        }

    def approve(self, task_id: str, reviewer_id: str, token: str) -> bool:
        """Aprova a execução mediante validação criptográfica estrita do token."""
        return self.queue.approve(task_id, reviewer_id=reviewer_id, token=token)

    def reject(self, task_id: str, reviewer_id: str, token: str, reason: str = '') -> bool:
        """Rejeita a execução mediante validação criptográfica estrita do token."""
        return self.queue.reject(task_id, reviewer_id=reviewer_id, token=token, reason=reason)

    def step(self, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Executa um ciclo unitário de consumo da fila, despacho e handoff."""
        worker_id = owner or f'worker-{uuid.uuid4().hex[:8]}'
        task = self.queue.claim(owner=worker_id)
        if not task:
            return None

        connector = self.connectors.get(task.target_agent)
        if not connector:
            err_msg = f'Conector não registrado para o agente {task.target_agent}'
            self.queue.fail(task.task_id, owner=worker_id, error_message=err_msg, is_transient=False)
            return {'task_id': task.task_id, 'status': 'failed', 'error': err_msg}

        try:
            result = connector.dispatch(task)
            self.queue.finish(task.task_id, owner=worker_id, result=result)

            # Verificar se há próximo especialista na cadeia (handoff automático)
            remaining = list(task.metadata.get('remaining_chain', []))
            if remaining:
                next_agent = remaining.pop(0)
                next_meta = dict(task.metadata)
                next_meta['remaining_chain'] = remaining
                next_meta['previous_task_id'] = task.task_id

                # Encaminhar a saída do anterior como entrada autorizada do próximo
                handoff_input = {
                    'origem': task.target_agent,
                    'resultado_anterior': result,
                    'contexto_inicial': task.authorized_input,
                }

                handoff_task = BridgeTask(
                    task_id=f'task-{uuid.uuid4().hex[:12]}',
                    correlation_id=task.correlation_id,
                    source_agent=task.target_agent,
                    target_agent=next_agent,
                    authorized_input=handoff_input,
                    human_approval_required=False,
                    metadata=next_meta,
                )
                self.queue.submit(handoff_task)

            return {
                'task_id': task.task_id,
                'correlation_id': task.correlation_id,
                'agent': task.target_agent,
                'status': 'done',
                'result': result,
            }

        except Exception as exc:
            is_transient = isinstance(exc, (RuntimeError, TimeoutError))
            self.queue.fail(
                task.task_id,
                owner=worker_id,
                error_message=f'{type(exc).__name__}: {str(exc)}',
                is_transient=is_transient,
            )
            return {
                'task_id': task.task_id,
                'correlation_id': task.correlation_id,
                'agent': task.target_agent,
                'status': 'retryable' if is_transient and task.attempt_count < task.max_attempts else 'failed',
                'error': str(exc),
            }

    def run_until_idle(self, max_steps: int = 25) -> List[Dict[str, Any]]:
        """Executa sucessivos passos da fila até que não haja mais tarefas prontas."""
        reports = []
        for _ in range(max_steps):
            report = self.step()
            if not report:
                break
            reports.append(report)
        return reports

    def consolidate(self, correlation_id: str) -> Dict[str, Any]:
        """Consolida todo o histórico, resultados e auditoria de uma correlação."""
        tasks = self.queue.get_tasks_by_correlation(correlation_id)
        events = self.queue.get_events(correlation_id)

        steps_summary = []
        final_result = None

        for t in tasks:
            step_info = {
                'task_id': t.task_id,
                'source': t.source_agent,
                'target': t.target_agent,
                'state': t.state,
                'attempts': t.attempt_count,
                'authorized_input': t.authorized_input,
                'result': t.result,
                'errors': t.errors,
                'completed': t.state == 'done',
            }
            steps_summary.append(step_info)
            if t.result:
                final_result = t.result

        is_complete = bool(tasks and all(t.state == 'done' for t in tasks) and not any(t.metadata.get('remaining_chain', [])))

        return {
            'correlation_id': correlation_id,
            'total_tasks': len(tasks),
            'completed_tasks': sum(1 for t in tasks if t.state == 'done'),
            'is_complete': is_complete,
            'steps': steps_summary,
            'final_result': final_result,
            'audit_events': [e.to_dict() for e in events],
        }

    def close(self) -> None:
        self.queue.close()
