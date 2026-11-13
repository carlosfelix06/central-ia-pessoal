"""Motor MDM & Golden Record — Arquitetura de Governança e Visão Única 360° do Cliente.

Implementa em 5 etapas a esteira completa de Master Data Management (MDM) com zero
dependências externas (stdlib Python 3.11+) e conformidade nativa LGPD / Fail-Closed:
1. Fontes de Dados Descentralizadas (ERPs da holding, CRMs, Legados, Cadastros por Produto).
2. Ingestão & ETL/ELT com metadados de origem e linhagem.
3. Esteira de Qualidade e Tratamento: Higienização/Padronização, Deduplicação & Match
   (modelo determinístico + probabilístico adaptado do padrão Fellegi-Sunter/Splink),
   Regras de Sobrevivência (Survivorship) e fila de Administração de Dados (Stewardship).
4. Motor MDM & Golden Record Hub com suporte aos 3 modelos de sincronismo
   (Coexistência, Centralizado e Consolidação).
5. Consumo & Sincronismo: Barramento 360° LGPD e write-back para sistemas legados.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

SYNC_MODES = ("coexistencia", "centralizado", "consolidacao")
SURVIVORSHIP_STRATEGIES = ("confianca_recencia", "recencia_primeiro", "completude_primeiro")

DEFAULT_SOURCE_TRUST: dict[str, int] = {
    "erp": 100,
    "crm": 85,
    "cadastro_produto": 75,
    "legado": 60,
}

ABBREVIATION_MAP = {
    "R": "RUA",
    "R.": "RUA",
    "AV": "AVENIDA",
    "AV.": "AVENIDA",
    "AL": "ALAMEDA",
    "AL.": "ALAMEDA",
    "PCA": "PRACA",
    "TRV": "TRAVESSA",
    "ROD": "RODOVIA",
    "APT": "APARTAMENTO",
    "APTO": "APARTAMENTO",
    "CJ": "CONJUNTO",
    "SL": "SALA",
    "N": "N",
}


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(raw_name: str) -> str:
    """Padroniza nomes removendo acentos, pontuação e espaços excedentes."""
    cleaned = _strip_accents(raw_name or "").upper()
    cleaned = re.sub(r"[^A-Z0-9\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def is_valid_cpf(digits: str) -> bool:
    """Valida dígito verificador módulo 11 de CPF brasileiro."""
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for check_pos in (9, 10):
        total = sum(int(digits[i]) * ((check_pos + 1) - i) for i in range(check_pos))
        expected = (total * 10) % 11
        expected = 0 if expected == 10 else expected
        if int(digits[check_pos]) != expected:
            return False
    return True


def is_valid_cnpj(digits: str) -> bool:
    """Valida dígito verificador módulo 11 de CNPJ brasileiro."""
    if len(digits) != 14 or len(set(digits)) == 1:
        return False
    weights_first = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    weights_second = (6, *weights_first)
    for weights, check_pos in ((weights_first, 12), (weights_second, 13)):
        total = sum(int(digits[i]) * weights[i] for i in range(check_pos))
        rem = total % 11
        expected = 0 if rem < 2 else 11 - rem
        if int(digits[check_pos]) != expected:
            return False
    return True


def normalize_document(raw_doc: str) -> tuple[str, bool, str]:
    """Retorna (digitos_normalizados, valido, tipo_documento)."""
    digits = _digits_only(raw_doc)
    if len(digits) == 11:
        return digits, is_valid_cpf(digits), "CPF"
    if len(digits) == 14:
        return digits, is_valid_cnpj(digits), "CNPJ"
    return digits, False, "DESCONHECIDO"


def mask_document_lgpd(digits: str) -> str:
    """Aplica mascaramento LGPD preservando apenas dígitos não identificadores."""
    clean = _digits_only(digits)
    if len(clean) == 11:
        return f"***.{clean[3:6]}.{clean[6:9]}-**"
    if len(clean) == 14:
        return f"**.{clean[2:5]}.{clean[5:8]}/{clean[8:12]}-**"
    return "***" if clean else ""


def normalize_email(raw_email: str) -> tuple[str, bool]:
    """Padroniza e-mail em minúsculas e valida estrutura sintática."""
    email = (raw_email or "").strip().lower()
    valid = bool(re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email))
    return (email if valid else ""), valid


def normalize_phone(raw_phone: str) -> str:
    """Normaliza telefones brasileiros para formato E.164 (+55DDNNNNNNNNN)."""
    digits = _digits_only(raw_phone)
    if not digits:
        return ""
    if digits.startswith("55") and len(digits) in (12, 13):
        return f"+{digits}"
    if len(digits) in (10, 11):
        return f"+55{digits}"
    return f"+{digits}" if len(digits) >= 8 else ""


def normalize_address(raw_address: str) -> str:
    """Padroniza logradouros expandindo abreviações comuns."""
    cleaned = _strip_accents(raw_address or "").upper()
    cleaned = re.sub(r"[,.;\-/]+", " ", cleaned)
    tokens = [ABBREVIATION_MAP.get(tok, tok) for tok in cleaned.split()]
    return " ".join(tokens)


def sha256_fingerprint(text: str) -> str:
    """Gera fingerprint criptográfico imutável para linhagem e trilha LGPD."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawCustomerRecord:
    """Etapa 1: Registro bruto originado em sistemas descentralizados da holding."""

    source_system: str
    source_record_id: str
    holding_company: str
    full_name: str
    document: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    updated_at: str = "2026-01-01T00:00:00Z"
    product_line: str = "geral"


@dataclass(frozen=True)
class CleanedCustomerRecord:
    """Etapa 3a: Registro após higienização, padronização e cálculo de qualidade."""

    raw: RawCustomerRecord
    source_category: str
    trust_weight: int
    norm_name: str
    norm_document: str
    document_type: str
    document_valid: bool
    norm_email: str
    norm_phone: str
    norm_address: str
    completeness_ratio: float
    quality_score: int
    anomalies: tuple[str, ...]
    record_fingerprint: str


@dataclass(frozen=True)
class AttributeProvenance:
    """Rastreia qual sistema venceu a regra de sobrevivência por atributo."""

    field: str
    chosen_value: str
    winning_source: str
    winning_record_id: str
    reason: str


@dataclass(frozen=True)
class StewardshipCase:
    """Caso encaminhado para Administração de Dados (Stewardship) humana."""

    case_id: str
    reason: str
    match_score: float
    left_record_id: str
    right_record_id: str
    suggested_action: str


@dataclass(frozen=True)
class SyncBackPayload:
    """Etapa 5: Evento de retorno para sincronizar sistemas legados e operacionais."""

    golden_id: str
    sync_mode: str
    target_system: str
    holding_company: str
    source_record_id: str
    changed_fields: dict[str, str]


@dataclass(frozen=True)
class GoldenRecord:
    """Etapa 4: Registro Único do Cliente consolidado no MDM Hub."""

    golden_id: str
    sync_mode: str
    full_name: str
    document_masked: str
    document_fingerprint: str
    document_type: str
    email: str
    phone: str
    address: str
    holding_companies: tuple[str, ...]
    source_systems: tuple[str, ...]
    lineage_ids: tuple[str, ...]
    match_confidence: float
    quality_score: int
    provenance: tuple[AttributeProvenance, ...]

    def to_api_360(self, *, mask_pii: bool = True) -> dict[str, Any]:
        """Etapa 5a: Serialização para Barramento de APIs & Microsserviços (Visão 360°)."""
        email_out = self.email
        phone_out = self.phone
        if mask_pii:
            if "@" in email_out:
                local, domain = email_out.split("@", 1)
                email_out = f"{local[:2]}***@{domain}"
            if len(phone_out) >= 6:
                phone_out = f"{phone_out[:5]}****{phone_out[-2:]}"
        return {
            "golden_id": self.golden_id,
            "sync_mode": self.sync_mode,
            "cliente": {
                "nome_padronizado": self.full_name,
                "documento": self.document_masked,
                "documento_tipo": self.document_type,
                "documento_sha256": self.document_fingerprint[:16],
                "email": email_out,
                "telefone": phone_out,
                "endereco": self.address,
            },
            "holding": {
                "empresas_relacionadas": list(self.holding_companies),
                "fontes_consolidadas": list(self.source_systems),
                "quantidade_origens": len(self.lineage_ids),
            },
            "governanca": {
                "match_confidence": round(self.match_confidence, 4),
                "quality_score": self.quality_score,
                "linhagem_ids": list(self.lineage_ids),
            },
        }


def _categorize_source(source_system: str) -> str:
    lowered = source_system.lower()
    if "erp" in lowered or "sap" in lowered or "totvs" in lowered:
        return "erp"
    if "crm" in lowered or "salesforce" in lowered or "hubspot" in lowered:
        return "crm"
    if "prod" in lowered or "cartao" in lowered or "credito" in lowered:
        return "cadastro_produto"
    return "legado"


def sanitize_and_standardize(
    record: RawCustomerRecord,
    source_trust: dict[str, int] | None = None,
) -> CleanedCustomerRecord:
    """Executa a subetapa de Higienização, Padronização e Auditoria de Qualidade."""
    trust_map = source_trust or DEFAULT_SOURCE_TRUST
    category = _categorize_source(record.source_system)
    trust_weight = trust_map.get(category, 60)

    anomalies: list[str] = []
    norm_name = normalize_name(record.full_name)
    if not norm_name:
        anomalies.append("nome_ausente")

    norm_doc, doc_valid, doc_type = normalize_document(record.document)
    if record.document and not doc_valid:
        anomalies.append("documento_invalido")
    elif not record.document:
        anomalies.append("documento_ausente")

    norm_email, email_valid = normalize_email(record.email)
    if record.email and not email_valid:
        anomalies.append("email_invalido")
    elif not record.email:
        anomalies.append("email_ausente")

    norm_phone = normalize_phone(record.phone)
    if record.phone and not norm_phone:
        anomalies.append("telefone_invalido")
    elif not record.phone:
        anomalies.append("telefone_ausente")

    norm_address = normalize_address(record.address)
    if not norm_address:
        anomalies.append("endereco_ausente")

    populated = sum(bool(v) for v in (norm_name, norm_doc if doc_valid else "", norm_email, norm_phone, norm_address))
    completeness = populated / 5.0

    # Penaliza campos inválidos mais severamente do que simples ausência
    invalid_penalty = sum(12 for item in anomalies if item.endswith("_invalido"))
    quality_score = max(0, min(100, round(completeness * 100) - invalid_penalty))

    fingerprint = sha256_fingerprint(
        "|".join((record.source_system, record.source_record_id, norm_name, norm_doc, norm_email))
    )
    return CleanedCustomerRecord(
        raw=record,
        source_category=category,
        trust_weight=trust_weight,
        norm_name=norm_name,
        norm_document=norm_doc,
        document_type=doc_type,
        document_valid=doc_valid,
        norm_email=norm_email,
        norm_phone=norm_phone,
        norm_address=norm_address,
        completeness_ratio=completeness,
        quality_score=quality_score,
        anomalies=tuple(anomalies),
        record_fingerprint=fingerprint,
    )


def compute_match_score(left: CleanedCustomerRecord, right: CleanedCustomerRecord) -> tuple[float, str]:
    """Calcula probabilidade de match determinístico/probabilístico entre dois registros."""
    shared_contact = bool(
        (left.norm_email and left.norm_email == right.norm_email)
        or (left.norm_phone and left.norm_phone == right.norm_phone)
    )
    if left.document_valid and right.document_valid:
        if left.norm_document == right.norm_document:
            return 1.0, "deterministico_documento"
        return (0.65 if shared_contact else 0.0), (
            "conflito_documento" if shared_contact else "distinto_documento"
        )

    name_sim = SequenceMatcher(None, left.norm_name, right.norm_name).ratio() if (left.norm_name and right.norm_name) else 0.0
    email_sim = 1.0 if (left.norm_email and left.norm_email == right.norm_email) else 0.0
    phone_sim = 1.0 if (left.norm_phone and left.norm_phone == right.norm_phone) else 0.0

    left_addr_tokens = set(left.norm_address.split())
    right_addr_tokens = set(right.norm_address.split())
    if left_addr_tokens and right_addr_tokens:
        addr_sim = len(left_addr_tokens & right_addr_tokens) / len(left_addr_tokens | right_addr_tokens)
    else:
        addr_sim = 0.0

    # Pesos calibrados estilo Fellegi-Sunter simplificado
    weighted = (0.45 * name_sim) + (0.25 * email_sim) + (0.20 * phone_sim) + (0.10 * addr_sim)
    return round(weighted, 4), "probabilistico_composto"


def _parse_iso_timestamp(ts: str) -> float:
    cleaned = (ts or "").strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _surviving_attribute(
    cluster: list[CleanedCustomerRecord],
    attr_getter: Any,
    field_name: str,
    strategy: str,
) -> AttributeProvenance:
    """Etapa 3c: Aplica Regra de Sobrevivência & Fusão (Survivorship) atributo a atributo."""
    candidates = [(rec, attr_getter(rec)) for rec in cluster if attr_getter(rec)]
    if not candidates:
        return AttributeProvenance(field_name, "", "nenhum", "", "nenhum_valor_valido")

    def sort_key(item: tuple[CleanedCustomerRecord, str]) -> tuple[float, float, float]:
        rec, _ = item
        ts = _parse_iso_timestamp(rec.raw.updated_at)
        trust = float(rec.trust_weight)
        comp = float(rec.completeness_ratio)
        if strategy == "recencia_primeiro":
            return (ts, trust, comp)
        if strategy == "completude_primeiro":
            return (comp, trust, ts)
        return (trust, ts, comp)

    winner, value = max(candidates, key=sort_key)
    reason_labels = {
        "confianca_recencia": f"confianca_fonte={winner.trust_weight}+recencia",
        "recencia_primeiro": f"mais_recente={winner.raw.updated_at}",
        "completude_primeiro": f"completude={round(winner.completeness_ratio * 100)}%",
    }
    return AttributeProvenance(
        field=field_name,
        chosen_value=value,
        winning_source=winner.raw.source_system,
        winning_record_id=winner.raw.source_record_id,
        reason=reason_labels[strategy],
    )


class MdmAuditStore:
    """Persistência operacional LGPD para linhagem de Golden Records sem PII em claro."""

    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mdm_runs (
                run_id TEXT PRIMARY KEY,
                executed_at TEXT NOT NULL,
                sync_mode TEXT NOT NULL,
                strategy TEXT NOT NULL,
                raw_count INTEGER NOT NULL,
                golden_count INTEGER NOT NULL,
                stewardship_count INTEGER NOT NULL,
                report_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mdm_lineage (
                golden_id TEXT NOT NULL,
                source_system TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                record_fingerprint TEXT NOT NULL,
                PRIMARY KEY (golden_id, source_system, source_record_id)
            );
            """
        )

    def record_run(
        self,
        run_id: str,
        sync_mode: str,
        strategy: str,
        raw_count: int,
        golden_records: Iterable[GoldenRecord],
        cleaned_map: dict[str, CleanedCustomerRecord],
        stewardship_count: int,
        report_sha256: str,
    ) -> None:
        gold_list = list(golden_records)
        with self.db:
            self.db.execute(
                "INSERT INTO mdm_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    sync_mode,
                    strategy,
                    raw_count,
                    len(gold_list),
                    stewardship_count,
                    report_sha256,
                ),
            )
            for gold in gold_list:
                for lid in gold.lineage_ids:
                    cleaned = cleaned_map[lid]
                    self.db.execute(
                        "INSERT OR REPLACE INTO mdm_lineage VALUES (?, ?, ?, ?)",
                        (
                            gold.golden_id,
                            cleaned.raw.source_system,
                            cleaned.raw.source_record_id,
                            cleaned.record_fingerprint,
                        ),
                    )

    def close(self) -> None:
        self.db.close()


@dataclass(frozen=True)
class MdmPipelineReport:
    """Relatório consolidado da execução completa das 5 etapas do MDM Hub."""

    run_id: str
    sync_mode: str
    strategy: str
    raw_records_count: int
    golden_records_count: int
    deduplication_rate: float
    avg_quality_before: float
    avg_quality_after: float
    golden_records: tuple[GoldenRecord, ...]
    stewardship_queue: tuple[StewardshipCase, ...]
    sync_back_events: tuple[SyncBackPayload, ...]
    source_quality_summary: dict[str, dict[str, Any]]
    report_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sync_mode": self.sync_mode,
            "strategy": self.strategy,
            "kpis": {
                "registros_brutos": self.raw_records_count,
                "golden_records": self.golden_records_count,
                "taxa_deduplicacao_pct": round(self.deduplication_rate * 100, 1),
                "qualidade_media_origens": round(self.avg_quality_before, 1),
                "qualidade_media_golden": round(self.avg_quality_after, 1),
                "casos_stewardship": len(self.stewardship_queue),
                "eventos_sync_retorno": len(self.sync_back_events),
            },
            "qualidade_por_fonte": self.source_quality_summary,
            "visao_360_clientes": [g.to_api_360(mask_pii=True) for g in self.golden_records],
            "stewardship": [asdict(s) for s in self.stewardship_queue],
            "sincronismo_retorno": [asdict(ev) for ev in self.sync_back_events],
            "report_sha256": self.report_sha256,
        }


class MdmHub:
    """Orquestrador MDM & Golden Record integrado à Equipe de BI & Governança."""

    def __init__(
        self,
        *,
        match_threshold: float = 0.72,
        stewardship_floor: float = 0.52,
        source_trust: dict[str, int] | None = None,
        audit_store: MdmAuditStore | None = None,
    ) -> None:
        self.match_threshold = match_threshold
        self.stewardship_floor = stewardship_floor
        self.source_trust = source_trust or dict(DEFAULT_SOURCE_TRUST)
        self.audit_store = audit_store
        self._raw_records: list[RawCustomerRecord] = []

    def ingest(self, records: Iterable[RawCustomerRecord]) -> int:
        """Etapa 2: Ingestão / ETL de registros das fontes da holding."""
        added = list(records)
        self._raw_records.extend(added)
        return len(added)

    def _candidate_pairs(self, cleaned: list[CleanedCustomerRecord]) -> set[tuple[int, int]]:
        """Blocking multi-chave para evitar comparacao O(N^2) mantendo homonimos e canais."""
        buckets: dict[str, list[int]] = {}
        for idx, rec in enumerate(cleaned):
            keys: list[str] = []
            if rec.document_valid:
                keys.append(f"doc:{rec.norm_document}")
            if rec.norm_email:
                keys.append(f"eml:{rec.norm_email}")
            if rec.norm_phone:
                keys.append(f"phn:{rec.norm_phone}")
            if rec.norm_name:
                tokens = rec.norm_name.split()
                keys.append(f"nm:{tokens[0][:3]}:{tokens[-1][:3]}")
            for k in keys:
                buckets.setdefault(k, []).append(idx)

        pairs: set[tuple[int, int]] = set()
        for indices in buckets.values():
            if len(indices) < 2:
                continue
            for pos_a in range(len(indices)):
                for pos_b in range(pos_a + 1, len(indices)):
                    a, b = indices[pos_a], indices[pos_b]
                    pairs.add((a, b) if a < b else (b, a))
        return pairs

    def _cluster_records(
        self, cleaned: list[CleanedCustomerRecord]
    ) -> tuple[list[list[CleanedCustomerRecord]], dict[int, float], list[StewardshipCase]]:
        """Etapa 3b: Deduplicação & Match com agrupamento por componentes conexos (Union-Find)."""
        n = len(cleaned)
        parent = list(range(n))
        pair_scores: dict[tuple[int, int], float] = {}
        stewardship_cases: list[StewardshipCase] = []

        def find(idx: int) -> int:
            while parent[idx] != idx:
                parent[idx] = parent[parent[idx]]
                idx = parent[idx]
            return idx

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        candidates_for_stewardship: list[tuple[int, int, float, str]] = []
        for i, j in sorted(self._candidate_pairs(cleaned)):
            score, mode = compute_match_score(cleaned[i], cleaned[j])
            if score >= self.match_threshold:
                union(i, j)
                pair_scores[(i, j)] = score
            elif self.stewardship_floor <= score < self.match_threshold or (
                mode == "conflito_documento"
                and cleaned[i].norm_name == cleaned[j].norm_name
                and bool(cleaned[i].norm_name)
            ):
                candidates_for_stewardship.append((i, j, score, mode))

        for i, j, score, mode in candidates_for_stewardship:
            if find(i) == find(j):
                continue
            case_reason = (
                "Conflito de CPF/CNPJ para o mesmo nome"
                if mode == "conflito_documento"
                else f"Match ambíguo ({round(score * 100, 1)}%)"
            )
            stewardship_cases.append(
                StewardshipCase(
                    case_id=f"STW-{i+1:03d}-{j+1:03d}",
                    reason=case_reason,
                    match_score=score,
                    left_record_id=cleaned[i].raw.source_record_id,
                    right_record_id=cleaned[j].raw.source_record_id,
                    suggested_action="Revisão humana do Data Steward antes de fundir registros",
                )
            )

        groups: dict[int, list[int]] = {}
        for idx in range(n):
            groups.setdefault(find(idx), []).append(idx)

        clusters: list[list[CleanedCustomerRecord]] = []
        cluster_confidence: dict[int, float] = {}
        for c_idx, indices in enumerate(groups.values()):
            clusters.append([cleaned[idx] for idx in indices])
            if len(indices) == 1:
                cluster_confidence[c_idx] = 1.0
            else:
                scores = [
                    pair_scores[(a, b)]
                    for idx_a, a in enumerate(indices)
                    for b in indices[idx_a + 1 :]
                    if (a, b) in pair_scores
                ]
                cluster_confidence[c_idx] = sum(scores) / len(scores) if scores else self.match_threshold

        return clusters, cluster_confidence, stewardship_cases

    def _build_sync_back(self, gold: GoldenRecord, cluster: list[CleanedCustomerRecord]) -> list[SyncBackPayload]:
        """Etapa 5b: Emite eventos de sincronismo de retorno conforme modelo escolhido."""
        if gold.sync_mode == "consolidacao":
            # Modelo Consolidação é somente leitura (MDM analítico/BI), sem write-back nas origens.
            return []

        events: list[SyncBackPayload] = []
        for rec in cluster:
            # No modo 'coexistencia', atualiza apenas campos vazios ou inválidos nas origens;
            # No modo 'centralizado', sobrescreve divergências impondo o Golden Record.
            force_overwrite = gold.sync_mode == "centralizado"
            delta: dict[str, str] = {}
            if gold.full_name and (not rec.norm_name or (force_overwrite and rec.norm_name != gold.full_name)):
                delta["full_name"] = gold.full_name
            if gold.email and (not rec.norm_email or (force_overwrite and rec.norm_email != gold.email)):
                delta["email"] = gold.email
            if gold.phone and (not rec.norm_phone or (force_overwrite and rec.norm_phone != gold.phone)):
                delta["phone"] = gold.phone
            if gold.address and (not rec.norm_address or (force_overwrite and rec.norm_address != gold.address)):
                delta["address"] = gold.address
            if delta:
                events.append(
                    SyncBackPayload(
                        golden_id=gold.golden_id,
                        sync_mode=gold.sync_mode,
                        target_system=rec.raw.source_system,
                        holding_company=rec.raw.holding_company,
                        source_record_id=rec.raw.source_record_id,
                        changed_fields=delta,
                    )
                )
        return events

    def run_pipeline(
        self,
        *,
        sync_mode: str = "coexistencia",
        strategy: str = "confianca_recencia",
    ) -> MdmPipelineReport:
        """Executa as 5 etapas completas sobre os registros ingeridos."""
        if sync_mode not in SYNC_MODES:
            raise ValueError(f"sync_mode inválido: {sync_mode!r}. Use um de {SYNC_MODES}.")
        if strategy not in SURVIVORSHIP_STRATEGIES:
            raise ValueError(f"strategy inválida: {strategy!r}. Use uma de {SURVIVORSHIP_STRATEGIES}.")
        if not self._raw_records:
            raise ValueError("Nenhum registro ingerido para processar no MDM Hub.")

        cleaned = [sanitize_and_standardize(rec, self.source_trust) for rec in self._raw_records]
        cleaned_map = {rec.raw.source_record_id: rec for rec in cleaned}

        # Resumo de qualidade por sistema de origem (Governança & Qualidade)
        per_source: dict[str, list[CleanedCustomerRecord]] = {}
        for rec in cleaned:
            per_source.setdefault(rec.raw.source_system, []).append(rec)
        source_summary = {
            src: {
                "registros": len(items),
                "qualidade_media": round(sum(i.quality_score for i in items) / len(items), 1),
                "anomalias_encontradas": sum(len(i.anomalies) for i in items),
            }
            for src, items in sorted(per_source.items())
        }

        clusters, conf_map, stewardship_cases = self._cluster_records(cleaned)
        golden_records: list[GoldenRecord] = []
        sync_back_events: list[SyncBackPayload] = []

        for idx, cluster in enumerate(clusters):
            prov_name = _surviving_attribute(cluster, lambda r: r.norm_name, "full_name", strategy)
            prov_doc = _surviving_attribute(
                cluster, lambda r: r.norm_document if r.document_valid else "", "document", strategy
            )
            prov_email = _surviving_attribute(cluster, lambda r: r.norm_email, "email", strategy)
            prov_phone = _surviving_attribute(cluster, lambda r: r.norm_phone, "phone", strategy)
            prov_address = _surviving_attribute(cluster, lambda r: r.norm_address, "address", strategy)

            doc_digits = prov_doc.chosen_value
            _, _, doc_type = normalize_document(doc_digits)
            doc_fp = sha256_fingerprint(doc_digits or prov_name.chosen_value)
            golden_id = f"GLD-{doc_fp[:10].upper()}"

            valid_fields = sum(
                bool(val)
                for val in (
                    prov_name.chosen_value,
                    doc_digits,
                    prov_email.chosen_value,
                    prov_phone.chosen_value,
                    prov_address.chosen_value,
                )
            )
            gold_quality = round((valid_fields / 5.0) * 100)

            gold = GoldenRecord(
                golden_id=golden_id,
                sync_mode=sync_mode,
                full_name=prov_name.chosen_value,
                document_masked=mask_document_lgpd(doc_digits),
                document_fingerprint=doc_fp,
                document_type=doc_type if doc_digits else "PENDENTE",
                email=prov_email.chosen_value,
                phone=prov_phone.chosen_value,
                address=prov_address.chosen_value,
                holding_companies=tuple(sorted({r.raw.holding_company for r in cluster})),
                source_systems=tuple(sorted({r.raw.source_system for r in cluster})),
                lineage_ids=tuple(r.raw.source_record_id for r in cluster),
                match_confidence=conf_map[idx],
                quality_score=gold_quality,
                provenance=(prov_name, prov_doc, prov_email, prov_phone, prov_address),
            )
            golden_records.append(gold)
            sync_back_events.extend(self._build_sync_back(gold, cluster))

        raw_count = len(cleaned)
        gold_count = len(golden_records)
        dedup_rate = (raw_count - gold_count) / raw_count
        avg_before = sum(r.quality_score for r in cleaned) / raw_count
        avg_after = sum(g.quality_score for g in golden_records) / gold_count

        run_seed = f"{sync_mode}:{strategy}:{raw_count}:{gold_count}:{datetime.now(timezone.utc).isoformat()}"
        run_id = f"MDM-{sha256_fingerprint(run_seed)[:10].upper()}"
        report_hash = sha256_fingerprint(
            json.dumps([g.golden_id for g in golden_records] + [list(g.lineage_ids) for g in golden_records])
        )

        if self.audit_store is not None:
            self.audit_store.record_run(
                run_id=run_id,
                sync_mode=sync_mode,
                strategy=strategy,
                raw_count=raw_count,
                golden_records=golden_records,
                cleaned_map=cleaned_map,
                stewardship_count=len(stewardship_cases),
                report_sha256=report_hash,
            )

        return MdmPipelineReport(
            run_id=run_id,
            sync_mode=sync_mode,
            strategy=strategy,
            raw_records_count=raw_count,
            golden_records_count=gold_count,
            deduplication_rate=dedup_rate,
            avg_quality_before=avg_before,
            avg_quality_after=avg_after,
            golden_records=tuple(golden_records),
            stewardship_queue=tuple(stewardship_cases),
            sync_back_events=tuple(sync_back_events),
            source_quality_summary=source_summary,
            report_sha256=report_hash,
        )


def build_holding_demo_records() -> list[RawCustomerRecord]:
    """Cria massa ilustrativa realista de uma Holding (ERP + CRM + Produto + Legado)."""
    return [
        RawCustomerRecord(
            source_system="ERP_SAP_Alfa",
            source_record_id="ERP-1001",
            holding_company="Alfa Varejo S.A.",
            full_name="Mariana Oliveira Santos",
            document="390.533.447-05",
            email="mariana.santos@alfamail.com.br",
            phone="(11) 98765-4321",
            address="Av. Paulista, 1578 - Bela Vista",
            updated_at="2026-10-15T14:20:00Z",
            product_line="Conta Digital",
        ),
        RawCustomerRecord(
            source_system="CRM_Salesforce_Beta",
            source_record_id="CRM-8842",
            holding_company="Beta Financeira",
            full_name="MARIANA O SANTOS",
            document="39053344705",
            email="MARIANA.SANTOS@ALFAMAIL.COM.BR",
            phone="",
            address="Avenida Paulista 1578",
            updated_at="2026-11-02T09:10:00Z",
            product_line="Cartao Black",
        ),
        RawCustomerRecord(
            source_system="Legado_Seguros_Gama",
            source_record_id="LEG-4420",
            holding_company="Gama Seguros",
            full_name="Mariana Oliveira S.",
            document="",
            email="mariana.santos@alfamail.com.br",
            phone="11987654321",
            address="",
            updated_at="2024-06-11T18:00:00Z",
            product_line="Seguro Residencial",
        ),
        RawCustomerRecord(
            source_system="ERP_TOTVS_Beta",
            source_record_id="ERP-2090",
            holding_company="Beta Financeira",
            full_name="João Pedro Ferreira Lima",
            document="52998224725",
            email="joao.lima@empresa.com",
            phone="(21) 99123-0044",
            address="R. da Assembleia, 10 - Centro",
            updated_at="2026-11-05T11:30:00Z",
            product_line="Credito Consignado",
        ),
        RawCustomerRecord(
            source_system="Cadastro_Produto_Cartao",
            source_record_id="PRD-7719",
            holding_company="Alfa Varejo S.A.",
            full_name="Joao Pedro F Lima",
            document="529.982.247-25",
            email="email_invalido_sem_arroba",
            phone="21991230044",
            address="Rua da Assembleia 10 Centro",
            updated_at="2026-09-01T08:00:00Z",
            product_line="Cartao Loja",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Motor MDM & Golden Record Hub — Central-IA")
    parser.add_argument("action", nargs="?", default="demo", choices=["demo"])
    parser.add_argument("--sync-mode", default="coexistencia", choices=SYNC_MODES)
    parser.add_argument("--strategy", default="confianca_recencia", choices=SURVIVORSHIP_STRATEGIES)
    args = parser.parse_args()

    store = MdmAuditStore(":memory:")
    try:
        hub = MdmHub(audit_store=store)
        hub.ingest(build_holding_demo_records())
        report = hub.run_pipeline(sync_mode=args.sync_mode, strategy=args.strategy)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
