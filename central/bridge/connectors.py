"""Conectores da Ponte Central para agentes especializados e ferramentas externas."""
from abc import ABC, abstractmethod
import time
from typing import Any, Callable, Dict, Optional
from .contract import BridgeTask


class BaseConnector(ABC):
    """Interface padronizada para executores e conectores de IA."""

    def __init__(self, name: str, *, mode: str = 'simulated') -> None:
        self.name = name
        self.mode = mode

    @abstractmethod
    def is_available(self) -> bool:
        """Indica se a interface do conector está pronta e operacional."""
        pass

    @abstractmethod
    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        """Executa ou encaminha a tarefa e retorna o resultado estruturado."""
        pass


class SimulatedConnector(BaseConnector):
    """Conector local determinístico para testes, validações e simulação dos especialistas."""

    def __init__(
        self,
        name: str,
        *,
        handler: Optional[Callable[[BridgeTask], Dict[str, Any]]] = None,
        transient_failures: int = 0,
    ) -> None:
        super().__init__(name, mode='simulated')
        self.handler = handler
        self.transient_failures = transient_failures

    def is_available(self) -> bool:
        return True

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise RuntimeError(f'Falha transitória simulada em {self.name}')

        if self.handler:
            return self.handler(task)

        return {
            'agent': self.name,
            'status': 'success',
            'summary': f'Executado com sucesso pelo agente {self.name}',
            'authorized_input': task.authorized_input,
            'completed_at': time.time(),
        }


class OpenAIConnector(BaseConnector):
    """Conector com OpenAI. Em modo simulado, não realiza chamadas de rede."""

    def __init__(self, *, mode: str = 'simulated', api_key: Optional[str] = None) -> None:
        super().__init__('openai', mode=mode)
        self.api_key = api_key

    def is_available(self) -> bool:
        if self.mode == 'simulated':
            return True
        return bool(self.api_key)

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        if self.mode == 'simulated':
            return {
                'provider': 'openai',
                'mode': 'simulated',
                'model': 'gpt-5.4-mini (mock)',
                'response': f'[SIMULAÇÃO OPENAI] Resposta gerada para tarefa {task.task_id}',
                'tokens': {'input': 25, 'output': 45},
            }
        raise PermissionError('Chamadas reais OpenAI bloqueadas por segurança sem autorização expressa.')


class GeminiConnector(BaseConnector):
    """Conector com Gemini. Em modo simulado, não realiza chamadas de rede."""

    def __init__(self, *, mode: str = 'simulated', api_key: Optional[str] = None) -> None:
        super().__init__('gemini', mode=mode)
        self.api_key = api_key

    def is_available(self) -> bool:
        if self.mode == 'simulated':
            return True
        return bool(self.api_key)

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        if self.mode == 'simulated':
            return {
                'provider': 'gemini',
                'mode': 'simulated',
                'model': 'gemini-2.5-flash (mock)',
                'response': f'[SIMULAÇÃO GEMINI] Resposta gerada para tarefa {task.task_id}',
                'tokens': {'input': 20, 'output': 40},
            }
        raise PermissionError('Chamadas reais Gemini bloqueadas por segurança sem autorização expressa.')


class GitHubConnector(BaseConnector):
    """Conector com GitHub (Issues, PRs, Actions). Opera por recibos seguros."""

    def __init__(self, *, mode: str = 'simulated', repo: str = 'carlosfelix06/central-ia-pessoal') -> None:
        super().__init__('github', mode=mode)
        self.repo = repo

    def is_available(self) -> bool:
        return True

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        action = task.authorized_input.get('action', 'issue_event')
        return {
            'platform': 'github',
            'repo': self.repo,
            'receipt_id': f'gh-recibo-{task.task_id[:8]}',
            'status': 'recorded',
            'action': action,
            'correlation_id': task.correlation_id,
            'summary': f'Recibo de evento registrado para {action} na issue de coordenação',
        }


class CopilotConnector(BaseConnector):
    """Conector com GitHub Copilot por eventos e automações de repositório."""

    def __init__(self, *, mode: str = 'simulated') -> None:
        super().__init__('copilot', mode=mode)

    def is_available(self) -> bool:
        return True

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        task_type = task.authorized_input.get('task_type', 'code_review')
        return {
            'agent': 'copilot',
            'mode': self.mode,
            'task_type': task_type,
            'status': 'dispatched_local_contract',
            'details': 'Sessão coordenada por recibo estruturado sem consumo de rede não autorizada',
        }


class AntigravityConnector(BaseConnector):
    """Conector com Google Antigravity (Protocolo de Workspace / Mailbox e CLI)."""

    def __init__(self, *, workspace_dir: Optional[str] = None) -> None:
        super().__init__('antigravity', mode='local_interface')
        self.workspace_dir = workspace_dir

    def is_available(self) -> bool:
        return True

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        return {
            'agent': 'antigravity',
            'mode': 'pair_programming_bridge',
            'action': 'task_execution',
            'task_id': task.task_id,
            'summary': f'Antigravity processou a tarefa {task.task_id} no workspace local',
            'input_handled': task.authorized_input,
        }


class CodexConnector(BaseConnector):
    """Conector com OpenAI Codex (Interface de artefatos/mailbox e CLI local)."""

    def __init__(self, *, mailbox_dir: Optional[str] = None) -> None:
        super().__init__('codex', mode='mailbox_interface')
        self.mailbox_dir = mailbox_dir

    def is_available(self) -> bool:
        return True

    def dispatch(self, task: BridgeTask) -> Dict[str, Any]:
        # O app desktop Codex não possui daemon de controle remoto;
        # A integração viável e segura é a troca de especificações em artefatos/arquivos.
        return {
            'agent': 'codex',
            'mode': 'artifact_mailbox',
            'task_id': task.task_id,
            'summary': f'Codex recebeu especificação via contrato estruturado de intercâmbio',
            'status': 'ready_for_execution',
        }
