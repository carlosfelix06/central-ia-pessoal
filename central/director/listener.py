"""Módulo de monitoramento contínuo (Ouvinte Local do Agente Antigravity)."""
import datetime as dt
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from .client import JiraClient, JiraSecurityError
from .local_agent import LocalAgentProtocol
from .models import AgentProfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("central.director.listener")

HEARTBEAT_FILE = os.path.join(".local-reports", "listener-heartbeat.json")


def get_timeout_target(now: dt.datetime, duration_hours: float = 4.0) -> dt.datetime:
    """Calcula o limite de expiração do ouvinte baseado na duração configurada."""
    return now + dt.timedelta(hours=duration_hours)


def update_heartbeat(
    duration_hours: float = 4.0,
    status: str = "LISTENING",
    interval_minutes: int = 15,
    timeout_target: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Registra batimento cardíaco (heartbeat) comprovando que o ouvinte está ativo até o timeout."""
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    target = timeout_target or get_timeout_target(now, duration_hours=duration_hours)

    timeout_reached = now >= target
    if timeout_reached:
        status = "TIMEOUT_EXPIRED"

    os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
    payload = {
        "status": status,
        "agent": "DATA_ENGINEER",
        "last_heartbeat": now.isoformat(),
        "expires_at": target.isoformat(),
        "interval_minutes": interval_minutes,
        "timeout_reached": timeout_reached,
        "pid": os.getpid(),
    }

    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    return payload


def check_incoming_instructions(
    dry_run: bool = True,
    interval_minutes: int = 15,
    duration_hours: float = 4.0,
) -> Dict[str, Any]:
    """Executa a checagem no Jira em busca de novas instruções enquanto o timeout não for atingido."""
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    timeout_target = get_timeout_target(now, duration_hours=duration_hours)

    if now >= timeout_target:
        logger.info("Limite de execução atingido. Interrompendo novas ações agendadas.")
        hb = update_heartbeat(status="TIMEOUT_EXPIRED", interval_minutes=interval_minutes, timeout_target=timeout_target)
        return {
            "heartbeat": hb,
            "timeout_reached": True,
            "message": "Timeout atingido. Novas ações agendadas interrompidas.",
            "pending_count": 0,
            "pending_instructions": [],
        }

    heartbeat = update_heartbeat(duration_hours=duration_hours, status="LISTENING", interval_minutes=interval_minutes, timeout_target=timeout_target)
    client = JiraClient(dry_run=dry_run)
    protocol = LocalAgentProtocol(client)

    instructions = []
    auth_ok = True
    error_msg = ""

    try:
        pending = protocol.get_pending_orientations_for_agent(AgentProfile.DATA_ENGINEER)
        instructions = pending
    except JiraSecurityError as e:
        auth_ok = False
        error_msg = str(e)
        logger.debug("Consulta Jira operando em modo offline/sem credenciais: %s", e)

    return {
        "heartbeat": heartbeat,
        "timeout_reached": False,
        "auth_ok": auth_ok,
        "error": error_msg,
        "pending_count": len(instructions),
        "pending_instructions": instructions,
    }


def main() -> None:
    res = check_incoming_instructions(dry_run=True)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
