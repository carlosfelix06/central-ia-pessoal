# Equipe de Business Intelligence (BI) & Analytics — Configuração Default

A **Central-IA** opera por padrão com uma **Equipe de Business Intelligence (BI) & Data Analytics** composta por **6 Agentes Especialistas** coordenados por um **Diretor Executivo de BI (`BI_LEAD`)**.

## Composição da Equipe de BI (Default)

| ID do Agente | Papel na Equipe de BI | Escopo Técnico e Funcional |
| :--- | :--- | :--- |
| **`coordenador_bi`** *(Default)* | **Coordenador Executivo de BI** | Planejamento de demandas analíticas, priorização de KPIs corporativos, definição de metas e governança de dados. |
| **`engenheiro_dados`** | **Engenheiro de Dados & ETL** | Arquitetura de pipelines SQL/Python, modelagem dimensional (*Star Schema*), camadas *Medallion* (*Bronze/Silver/Gold*) e ingestão confiável. |
| **`analista_bi`** | **Analista de BI & Modelagem Semântica** | Construção de cubos analíticos, medidas DAX/MDX, análise de tendência, *cohort*, *churn* e indicadores operacionais. |
| **`especialista_dataviz`** | **Especialista em DataViz & Storytelling** | Design de painéis executivos (Power BI, Looker, Tableau), hierarquia visual e narrativas orientadas a decisão. |
| **`analytics_finops`** | **Analytics Translator & FinOps** | Tradução de desafios de negócio em hipóteses analíticas, cálculo de ROI, margem e controle atômico de custos de nuvem/API. |
| **`auditor_qualidade`** | **Auditor de Qualidade de Dados & LGPD** | Validação estatística de bases, reconciliação de dados, detecção de anomalias, linhagem e conformidade LGPD/segurança. |

## Perfis de Governança e Triagem (`central.director`)

* **`BI_LEAD` (Default):** Coordenação estratégica, distribuição de backlog analítico e homologação executiva.
* **`DATA_ENGINEER`:** Implementação e automação de pipelines de dados, filas e integrações.
* **`BI_ANALYST`:** Revisão técnica de consultas, modelos semânticos e versionamento.
* **`DATAVIZ_SPECIALIST`:** Documentação executiva, catálogos de indicadores e storytelling.
* **`DATA_QUALITY_AUDITOR`:** Auditoria de segurança (*Bandit*), privacidade (*Fail-Closed*) e conformidade.
