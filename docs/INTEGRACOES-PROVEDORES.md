> **Decisão CEO 04/11/2026:** pesquisar solução já existente no GitHub ANTES de desenvolver; a alternativa construída do zero exige decisão fundamentada na issue Jira. [Plano](PLANO-DESENVOLVIMENTO-REUSO-GITHUB.md) · [Catálogo de 20 candidatos](CATALOGO-SOLUCOES-GITHUB.md) · [Diretor](DIRETOR-EXECUTIVO-CLOUD.md). Jira, CEP v1.0 e marcos M1–M4 preservados. Trechos antigos abaixo são registros históricos, NÃO estado operacional ou autorização live.

# OpenAI e Gemini: rotas explícitas, execução supervisionada

Extensão local de `central.phase3`, sem recriar os seis especialistas. Os comandos
das Fases 1, 2 e 3 continuam independentes. `central.integrations` usa a mesma fila,
reservas, recuperação, cancelamento e aprovação, acrescentando rotas persistidas.
Nenhuma integração real, serviço contínuo ou fallback foi habilitado.

## Validar sem API

No PowerShell, na pasta do workspace:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\Test-Automation.ps1
.\.venv\Scripts\python.exe -m central.integrations --directory .local-reports/integrations-demo demo
```

`demo` força modo mock mesmo com credenciais e flags presentes. Os seis agentes
recebem automaticamente o pedido original e o resultado imediatamente anterior.
O relatório privado `report.json` contém entrada, saída, provedor, modelo, estado,
tentativas e orçamento. Respostas são determinísticas, sem análise feita por IA.
Os valores de consumo nessa demonstração são contabilidade simulada, não cobrança.
O cenário é uma proposta fictícia PJ de R$ 19.000 em Matriz-SP.

A demonstração alterna provedores com consentimento explícito porque seus dados
são fictícios. O exemplo operacional `config/integrations-example.json` usa apenas
Gemini na cadeia Coordenador de BI → Analista de BI → Analytics & FinOps, sempre em modo mock.
Não foi configurado compartilhamento automático de dados privados entre provedores.

## Ambiente e dependências

Python 3.11, ambiente `.venv` próprio e dependências declaradas em
`requirements-dev.lock`. O SDK oficial é `google-genai==2.34.0`; `python-dotenv`
permite leitura seletiva de credenciais. São dependências opcionais do pacote
(`.[gemini]`) e fazem parte do conjunto de desenvolvimento para os testes simulados.
As fases antigas não passam a importar o SDK Gemini.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
```

Instalar pacotes baixa dependências; esses comandos não enviam prompts a modelos.

## Credenciais

Não há busca automática em outros projetos e não há cópia de `.env`.
`inspect-credentials --env-file` aceita um arquivo escolhido explicitamente e
informa somente presença de variáveis, conflitos de aliases e ausência de teste de
validade. Não carrega valores no processo nem consulta a API.

`GEMINI_API_KEY` é o nome preferido; `GOOGLE_API_KEY` é um alias aceito. Valores
divergentes bloqueiam a chamada. Uma fonte já presente no processo também não é
substituída silenciosamente pelo arquivo. Interpolação de variáveis é desativada.

Somente `work`, em modo live, com `--allow-real --env-file` explícitos, pode carregar
as três variáveis permitidas (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`)
na memória desse processo. Flags de autorização, modelos e orçamento escritos no
`.env` são ignorados. Não salva credenciais, não modifica o arquivo e não inclui
valores em mensagens de erro. A opção `--env-file` é dispensável com variáveis de
processo preparadas por um gerenciador de segredos. Não compartilhar saídas de
comandos que enumerem o ambiente ou mostrem o conteúdo do arquivo.

O armazenamento DPAPI e os scripts OpenAI anteriores permanecem separados.
`.env`, bancos, filas, relatórios privados, logs, backups, arquivos DPAPI e `.venv`
continuam fora do Git. O verificador inspeciona os arquivos candidatos e o conteúdo
efetivo do índice Git, incluindo padrões conhecidos de chaves Google/OpenAI/GitHub.
Essa verificação por padrões não substitui revisão humana ou um cofre de segredos.

## Rotas e comunicação automática

Cada etapa em `steps` declara `agent`, `provider`, `model`, `max_output_tokens` e,
quando necessário, `allow_cross_provider`. Os provedores aceitos são `openai` e
`gemini`; os seis identificadores existentes de agentes foram preservados.

Para passar de um provedor para outro, a etapa de destino precisa declarar
`allow_cross_provider: true`. O padrão é negar. O consentimento autoriza o envio do
pedido original e do resultado anterior ao provedor seguinte; não faz anonimização
nem classificação de dados. Revisar essa escolha antes de usar dados pessoais.
Falhas nunca alteram a rota nem acionam outro provedor.

Rotas, consentimento, limites e pedido compõem a identidade persistida. Repetir a
mesma chave e o mesmo conteúdo reutiliza a execução. Alterar a rota/conteúdo com a
mesma chave é rejeitado. O chamador deve reutilizar a chave em reenvios; gerar
identificadores novos para o mesmo pedido não é deduplicado semanticamente.

O banco da extensão tem política própria e não aceita abertura como fila legada.
Use uma pasta separada da `fase3-live`. Não há migração nem execução do pedido
pendente naquela pasta. Mudanças de política exigem revisão e não permitem
reabrir silenciosamente uma fila com limites diferentes.

## Modelos e estimativas revisados em 31/10/2026

Padrão Gemini: `gemini-3.5-flash-lite`, estável no catálogo público atual. A conta
do usuário e o acesso efetivo da chave não foram consultados. O adaptador OpenAI
existente continua com `gpt-5.4-mini-2026-03-17`; o alias previamente aceito também
permanece no catálogo local. Não foi usado endpoint autenticado de listagem.

Para Gemini textual standard, a referência paga é US$ 0,30 por milhão de tokens
de entrada e US$ 2,50 por milhão de saída, incluindo pensamento. Exemplo: 1.000 de
entrada e 256 de saída estimam US$ 0,000940 por chamada. Não inclui impostos ou
outros produtos. Não pressupor acesso gratuito ou que toda resposta caberá em
256 tokens; saídas truncadas são tratadas como incertas, sem reenvio automático.

O nível gratuito e o pago têm condições diferentes de uso dos dados para melhoria
dos produtos. Conferir plano, termos e privacidade da conta antes de enviar dados
privados. A integração não escolhe nem ativa um plano.

Fontes oficiais consultadas:

- [Modelo e estabilidade](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
- [Preços e condições dos níveis gratuito e pago](https://ai.google.dev/gemini-api/docs/pricing)
- [SDK oficial](https://googleapis.github.io/python-genai/)

### Referência adicional enviada pelo usuário: Interactions e Gemini 3.8

O exemplo com `client.interactions.create` usa o mesmo SDK, por outra interface.
A documentação atual recomenda Interactions para projetos novos e mantém
`generateContent` plenamente suportado. Este projeto existente usa
`client.models.generate_content`, com validação da resposta e consumo, para manter
o escopo textual supervisionado da integração entregue.

O exemplo é uma referência de configuração, sem comprovar acesso da chave ao
`gemini-3.8-flash`. Ler `os.environ` não carrega um arquivo `.env`. A busca Google,
o raciocínio alto e a saída de até 65.536 tokens exigem orçamento e avaliação de
dados próprios; não foram incorporados nem executados. O catálogo local continua
restrito e rejeita esse modelo enquanto sua política não for adicionada/revisada.

Uma futura migração precisa desativar armazenamento remoto com `store=false`,
rever o formato de uso/saída, custear separadamente buscas e preservar todos os
gates. Interactions armazena pedidos por padrão. Além disso, Gemini 3.8 Flash não
aceita o nível `minimal` usado pelo adaptador Flash-Lite; mudar só o nome do modelo
não é uma migração compatível. Nenhuma alteração de provedor/modelo é feita em
resposta a uma falha.

Fontes: [Interactions e armazenamento](https://ai.google.dev/gemini-api/docs/interactions-overview),
[capacidades Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
e [custos de geração e busca](https://ai.google.dev/gemini-api/docs/pricing).

## Limites e falhas

- `settings.budget_microusd`: teto compartilhado por fila, sem renovação automática.
  `provider_budgets`: subtetos OpenAI/Gemini dentro dele. Um milhão de micros = US$ 1.
  O exemplo reserva no máximo US$ 0,01 no total e US$ 0,005 por chamada.
- A reserva é atômica antes do despacho. Consumo considera o maior entre reserva
  e custo observado, preservando reservas de respostas incertas. Orçamento zero
  bloqueia antes de iniciar o processo de API. Limites são persistidos por fila;
  outras filas/máquinas/programas não compartilham esse saldo.
- Estimativa informativa usa aproximadamente quatro bytes por token; reserva usa
  bytes mais margem e o teto de saída. São heurísticas. Uso maior que a reserva ou
  que o limite de saída abre o circuito e pausa novos trabalhos daquela fila.
- RPM padrão: duas tentativas por minuto por provedor, persistidas e verificadas
  na mesma transação. Quando o limite é atingido, a tarefa aguarda sem consumir
  outra reserva. O trabalhador retorna; executá-lo novamente após o prazo retoma
  o trabalho. Não há serviço/agendador em segundo plano nesta entrega.
- Timeout padrão: dez segundos, com prazo de transporte e supervisão em processo
  separado. Cancelamento/timeout interrompem o filho local; não comprovam que o
  provedor deixou de processar ou cobrar uma chamada já enviada.
- SDK configurado com uma única tentativa; a fila controla no máximo duas no
  exemplo (até três pela política). Somente resposta HTTP 429 permite nova
  tentativa limitada na mesma rota, com espera persistida. Erros rejeitados são
  terminais; falhas ambíguas, crashes e timeouts ficam `uncertain`, sem reenvio.
- Após interrupção, checkpoint confirmado não é repetido. Lease vencida sem
  confirmação vira `uncertain`. Não apagar banco, recibo ou reserva para forçar
  repetição: investigar primeiro se houve execução remota.
- `drain` aceita no máximo 18 execuções por invocação e a cadeia contém até seis
  agentes distintos. Não existe próximo agente decidido pelo texto do modelo.

São controles locais preventivos, não um teto de faturamento Google/OpenAI.
Não há reconciliação com faturas nem coordenação de cotas entre máquinas.

## Auditoria, permissões e recuperação

`operations.sqlite3` guarda rotas, estados, códigos de eventos, consumo e decisões.
`private.sqlite3` guarda pedidos, mensagens e conteúdo de ações. Ambos precisam ser
preservados juntos; não são criptografados e a auditoria local é modificável por
quem tem acesso aos arquivos. O SDK não registra conteúdo/chaves no processo filho.
Exceções externas são reduzidas a códigos fixos, sem corpo HTTP ou cabeçalhos.

Propostas de e-mail, escrita de arquivo ou compromisso usam o mesmo fluxo de
aprovação já existente: `awaiting_approval` → revisão do conteúdo e fingerprint →
`approved` ou `rejected`; também existem expiração e revogação. Mesmo aprovada, a
ação não é executada: não foram instalados conectores externos. Identidade do
revisor ainda é uma declaração local, sem autenticação de produção.

Depois de uma interrupção, reabrir a mesma pasta com a mesma configuração e usar
`status`/`work` preserva IDs e rotas. `cancel --run` sinaliza cancelamento e revoga
aprovações pendentes. Estados incertos e bloqueios financeiros exigem investigação
humana; não há um comando para ignorar a trava ou resetar o orçamento.

## Antes de qualquer teste real

Ainda é necessária autorização expressa para o primeiro envio, com texto fictício,
provedor, modelo, teto e estimativa definidos. Conferir acesso e limites da conta,
condições de privacidade e preços; preparar fonte segura de credenciais sem expô-la.
Criar uma fila nova dedicada com `settings.mode: live`. A liberação exige
`--allow-real`, `INTEGRATIONS_ENABLE_REAL=true` e `INTEGRATIONS_PRICES_ACK=true`.
Uma rota OpenAI também exige os gates OpenAI legados (`P3_ENABLE_OPENAI` e
`P3_PRICES_ACK`). Nenhum desses passos foi autorizado/executado nesta consolidação.

Os testes simulados não comprovam compatibilidade real do modelo, latência,
cobrança, saldo, rate limits da conta ou condições contratuais. Produção ainda
precisa de retenção/backup seguro, controle de acesso, autenticação de aprovações,
monitoramento, reconciliação de consumo e ensaio autorizado do protocolo real.
