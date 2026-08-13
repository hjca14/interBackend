# CONTEXT.md — interBackend

Este documento existe para que qualquer agente (humano ou IA) consiga
continuar o projeto **sem depender do histórico de nenhuma conversa
anterior**. Leia este arquivo e a documentação em `docs/` antes de editar
qualquer coisa.

## Identidade do projeto

Produto: **InterBridge** — um sistema de interfone/porteiro conectado.

Três repositórios compõem o produto:

| Repositório | Responsabilidade |
| --- | --- |
| [`interBridge`](https://github.com/hjca14/interBridge) | Firmware do dispositivo físico (ESP32). Dono do protocolo de comunicação dispositivo↔nuvem. |
| [`interapp`](https://github.com/hjca14/interapp) | Aplicativo Flutter usado pelo usuário final. Nunca se conecta diretamente ao broker MQTT. |
| [`interBackend`](https://github.com/hjca14/interBackend) (este repositório) | Backend e infraestrutura AWS: API HTTPS, Lambdas, DynamoDB, AWS IoT Core. |

**Regra importante:** esta tarefa (Fase 1A) trabalha exclusivamente no
`interBackend`. Os outros dois repositórios foram apenas consultados
(somente leitura) para alinhamento e **não foram alterados**.

## Decisões arquiteturais

- AWS como plataforma de nuvem, região inicial `sa-east-1` (São Paulo).
- AWS IoT Core como broker MQTT.
- MQTT/TLS entre dispositivo (`interBridge`) e AWS.
- Certificado X.509 individual por dispositivo (nunca gerado ou commitado
  neste repositório).
- API HTTPS (API Gateway + Lambda) entre `interapp` e o backend.
- O aplicativo **nunca** acessa o broker MQTT diretamente — sempre via API
  HTTPS.
- AWS IoT **Basic Ingest** para eventos do dispositivo, quando aplicável
  (evita round-trip por um tópico intermediário).
- DynamoDB como banco de dados planejado (modelo de tabelas ainda **não**
  fechado — ver "Pendências" abaixo).
- API Gateway (HTTP API) + Lambda como camada de backend planejada.
- Ambiente inicial: `dev`. Nenhum outro ambiente está configurado ainda.
- Uma única conta AWS por enquanto. Sem AWS Organizations. Sem IAM
  Identity Center. Plano gratuito (Free Tier) preservado deliberadamente.
- Infraestrutura gerenciada inteiramente por AWS CDK v2 (Python).
- Python usado tanto para o CDK quanto para as futuras Lambdas.
- Repositório **público** — nenhum segredo, certificado, chave privada ou
  dado pessoal pode ser commitado. Ver `scripts/check_secrets.py` e
  `.gitignore`.

## Contrato do protocolo v1 (resumo — não é a fonte oficial)

A fonte oficial e autoritativa do protocolo é
**`interBridge/docs/communication-protocol.md`**. Este resumo existe apenas
para orientação rápida de quem trabalha no backend e **nunca deve ser
copiado/expandido a ponto de competir com o documento oficial** — em caso
de dúvida ou divergência, o documento do `interBridge` prevalece.

- `protocol_version = 1` em toda mensagem custom.
- `command_id`: exatamente 32 caracteres hexadecimais minúsculos, sem
  prefixo.
- `issued_at` e `expires_at`: segundos Unix epoch (inteiros), **não**
  strings ISO-8601. O **backend** gera esses timestamps ao emitir um
  comando (é a autoridade sobre `issued_at`/`expires_at`); o dispositivo
  ainda valida a janela de validade e rejeita comandos expirados/inválidos
  de forma independente.
- Timestamp de evento (dispositivo → nuvem): string ISO-8601 UTC, ex.
  `2026-08-11T14:30:25Z`.
- Estados do intercomunicador: `IDLE`, `RINGING`, `OFF_HOOK`, `IN_CALL`,
  `ERROR`.
- Formato do QR code de reivindicação (claim):
  `interbridge://claim?v=1&device_id=ib-<32hex>&claim_code=<segredo>`.
- Comandos remotos proibidos continuam proibidos: `ENTER_PROVISIONING` e
  `FACTORY_RESET` exigem confirmação física no dispositivo e não são
  executáveis remotamente no protocolo v1.
- `claim_code` é um **segredo**, mas **não é uma credencial permanente** —
  é um segredo de reivindicação de posse do produto, distinto do
  certificado X.509 permanente do dispositivo.

## Estado atual (Fase 1A — concluída nesta tarefa)

### O que foi implementado

- Estrutura completa do projeto CDK v2 em Python (`app.py`,
  `infrastructure/`, `tests/`, `docs/`, `.github/workflows/ci.yml`).
- Configuração tipada e centralizada (`infrastructure/config/`):
  `EnvironmentConfig`, `get_environment_config()`, `resource_name()`,
  `stack_id()`. Região padrão `sa-east-1`, ambiente padrão/único `dev`,
  Account ID nunca hardcoded (lido de `CDK_DEFAULT_ACCOUNT`).
- Quatro stacks (`DataStack`, `IoTStack`, `ApiStack`,
  `ObservabilityStack`), cada uma aplicando as tags padrão (`Project`,
  `Environment`, `ManagedBy`, `Repository`) e a tag `Component`
  correspondente (`database`, `iot`, `api`, `monitoring`).
- Verificação local de segredos (`scripts/check_secrets.py`), executada
  também em CI.
- `.gitignore` cobrindo certificados, chaves privadas, credenciais AWS,
  artefatos de provisioning, exports de tabelas, `.env`, `cdk.out/`, etc.
- Suíte de testes (`tests/unit/`, `tests/snapshot/`) cobrindo configuração,
  naming, tags, ausência de recursos proibidos, ausência de segredos no
  template sintetizado, e síntese completa do app sem credenciais AWS.
- `README.md`, `CONTEXT.md` e documentação em `docs/`.
- GitHub Actions (`ci.yml`) rodando lint, formatação, tipagem, testes,
  cobertura, `cdk synth` e verificação de segredos — sem acessar a conta
  AWS.

### O que é apenas estrutura (nenhum recurso AWS real)

- `DataStack`, `IoTStack`, `ApiStack` e `ObservabilityStack` **não
  provisionam nenhum recurso AWS** nesta fase. Cada uma aplica apenas tags
  à própria stack. Isso é intencional: o modelo de dados, os contratos da
  API e a estratégia de observabilidade ainda não estão fechados (ver
  "Pendências" abaixo), e criar recursos "de mentira" apenas para
  preencher as stacks foi explicitamente evitado.
- `lambdas/` não contém nenhuma função implementada.
- `infrastructure/constructs/` está vazio — nenhum padrão reutilizável
  foi necessário ainda.

### Comandos que funcionam (validados localmente nesta tarefa)

- `python -m venv .venv` + `pip install -r requirements.txt -r requirements-dev.txt`
- `pytest` (35 testes, todos passando)
- `ruff check .` / `ruff format --check .`
- `mypy infrastructure`
- `python app.py` com `CDK_OUTDIR` customizado — sintetiza as 4 stacks sem
  credenciais AWS (`environment: aws://unknown-account/sa-east-1` no
  manifest).
- `cdk synth` via CDK CLI instalado localmente via `npx aws-cdk@2`.

Ver a seção "Relatório final" da tarefa que criou este estado (histórico de
conversa) para os números exatos de testes/lint executados nesta rodada —
mas **não confie cegamente nisso**: rode os comandos acima novamente antes
de assumir que o estado ainda é válido.

### O que NÃO foi feito (deliberadamente, fora do escopo da Fase 1A)

- `cdk bootstrap`: **não executado**.
- `cdk deploy`: **não executado**.
- Nenhum recurso AWS real foi criado, alterado ou removido.
- Nenhuma access key foi criada.
- Nenhum GitHub OIDC configurado.
- Nenhum Cognito (ou outro provedor de autenticação) configurado.
- Nenhum certificado X.509 gerado.
- Nenhum scanner de QR ou cliente MQTT implementado no app.

## Fases planejadas

Ver `docs/phases.md` para critérios de conclusão detalhados de cada fase.

```text
Fase 1A — fundação do backend e CDK                 [concluída nesta tarefa]
Fase 1B — infraestrutura mínima AWS                 [não iniciada]
Fase 1C — primeiro dispositivo MQTT/TLS             [não iniciada]
Fase 1D — ingestão, persistência e observabilidade  [não iniciada]
Fase 2  — autenticação do usuário e API do app      [não iniciada]
Fase 3  — claim por QR e provisioning                [não iniciada]
Fase 4  — integração completa do interapp            [não iniciada]
Fase 5  — fleet provisioning, OTA e produção          [não iniciada]
```

## Pendências e decisões abertas

- **Modelo definitivo das tabelas DynamoDB**: tabela única vs. múltiplas
  tabelas, chaves de partição/ordenação, GSIs, estratégia de idempotência
  de comandos.
- **Contratos exatos da API HTTPS** consumida pelo `interapp` (rotas,
  payloads, códigos de erro).
- **Autenticação do app**: mecanismo ainda não escolhido (Cognito é uma
  possibilidade, mas não foi decidido nem implementado).
- **Processo seguro de emissão de certificados** para os dispositivos.
- **Fleet Provisioning**: fluxo exato ainda não implementado.
- **Estratégia DEV/PROD futura**: hoje existe apenas `dev`; separação
  formal de ambientes/contas ainda não decidida.
- **Retenção de eventos e logs**: períodos de retenção ainda não
  definidos.
- **Limites e alarmes** operacionais (CloudWatch) ainda não definidos.
- **OTA** (atualização de firmware via AWS IoT Jobs): não implementado.
- **Domínio e identidade comercial** (nome de domínio, marca) ainda não
  definidos.
- **Eventual separação de contas AWS** (ex.: dev vs. prod) não decidida.
- **Revisão jurídica do nome "InterBridge"** ainda não realizada.

## Regras para futuros agentes

1. Leia este `CONTEXT.md` e a documentação em `docs/` antes de editar
   qualquer coisa.
2. Preserve o protocolo oficial: a fonte de verdade é sempre
   `interBridge/docs/communication-protocol.md`. Não duplique nem
   diverja dele neste repositório.
3. Não crie sucesso falso: nunca implemente endpoints, recursos ou testes
   que aparentem funcionar sem de fato funcionarem.
4. Não faça deploy (`cdk bootstrap`/`cdk deploy`) sem autorização explícita
   e recente do responsável pelo projeto.
5. Nunca commite segredos, certificados, chaves privadas, Account IDs
   reais ou dados pessoais. Rode `python scripts/check_secrets.py` antes de
   commitar.
6. Não altere `interBridge` ou `interapp` sem solicitação explícita.
7. Atualize este `CONTEXT.md` depois de qualquer mudança arquitetural
   relevante.
8. Execute os testes (`pytest`, `ruff`, `mypy`, `cdk synth`) e relate os
   resultados honestamente — nunca invente resultados.
9. Preserve alterações do usuário: sempre rode `git status` antes de
   qualquer operação potencialmente destrutiva.
