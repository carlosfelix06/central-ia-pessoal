"""Ponte Central de comunicação e orquestração de tarefas e agentes."""
from .auth import ApprovalAuthenticator
from .contract import BridgeTask, BridgeEvent
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
from .orchestrator import BridgeOrchestrator

__all__ = [
    'ApprovalAuthenticator',
    'BridgeTask',
    'BridgeEvent',
    'BaseConnector',
    'SimulatedConnector',
    'OpenAIConnector',
    'GeminiConnector',
    'GitHubConnector',
    'CopilotConnector',
    'AntigravityConnector',
    'CodexConnector',
    'BridgeQueue',
    'BridgeOrchestrator',
]
