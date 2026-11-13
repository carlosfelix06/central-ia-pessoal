"""Módulo de autenticação verificável para portões de aprovação humana."""
import hashlib
import hmac
import os
import time
from typing import Optional


class ApprovalAuthenticator:
    """Validador criptográfico de aprovações humanas baseado em HMAC-SHA256."""

    def __init__(self, secret: Optional[bytes] = None) -> None:
        # Se nenhum segredo for fornecido, utiliza semente local segura gerada no processo
        self._secret = secret or os.urandom(32)

    def create_challenge(self, approval_id: str, task_id: str, action_type: str, payload_fingerprint: str) -> str:
        """Gera o desafio imutável vinculado estritamente à tarefa e ao payload da ação."""
        raw = f'{approval_id}:{task_id}:{action_type}:{payload_fingerprint}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def generate_token(self, reviewer_id: str, challenge: str, *, expires_at: Optional[float] = None) -> str:
        """Emite um token criptográfico de aprovação vinculando a identidade do revisor ao desafio."""
        if not reviewer_id or not isinstance(reviewer_id, str):
            raise ValueError('reviewer_id deve ser informado')
        if not challenge or not isinstance(challenge, str):
            raise ValueError('challenge deve ser informado')

        expiry = expires_at if expires_at is not None else (time.time() + 900)  # 15 minutos padrão
        message = f'{reviewer_id}:{challenge}:{expiry:.0f}'
        signature = hmac.new(self._secret, message.encode('utf-8'), hashlib.sha256).hexdigest()
        return f'{expiry:.0f}:{signature}'

    def verify_token(self, reviewer_id: str, challenge: str, token: str) -> bool:
        """Verifica a integridade, validade temporal e assinatura criptográfica do token de aprovação."""
        if not token or not isinstance(token, str) or ':' not in token:
            return False

        try:
            expiry_str, signature = token.split(':', 1)
            expiry = float(expiry_str)
        except (ValueError, TypeError):
            return False

        if time.time() > expiry:
            return False  # Token expirado

        expected_message = f'{reviewer_id}:{challenge}:{expiry:.0f}'
        expected_sig = hmac.new(self._secret, expected_message.encode('utf-8'), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected_sig)
