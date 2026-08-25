# Fases planejadas

Este documento registra as fases planejadas para o `interBackend`, seus
critérios de conclusão e dependências. Ver também `CONTEXT.md` para o
estado atual detalhado.

## Fase 1A — fundação CDK

- **Escopo:** estrutura do projeto CDK v2 em Python, configuração tipada de
  ambiente/região/tags, quatro stacks preparatórias (sem recursos reais ou
  com recursos estritamente seguros), testes, CI local reprodutível,
  documentação, proteção contra segredos.
- **Critério de conclusão:** `ruff check`, `ruff format --check`, `mypy`,
  `pytest` e `cdk synth` executam com sucesso localmente e em CI, sem
  acessar a conta AWS.
- **Dependências:** nenhuma (ponto de partida).
- **Não inclui:** `cdk bootstrap`, `cdk deploy`, qualquer recurso AWS real.

## Fase 1B.1 — base compartilhada do IoT

- **Escopo:** infraestrutura compartilhada mínima do AWS IoT Core,
  necessária para preparar a conexão do primeiro dispositivo de teste:
  Thing Type, Thing Group (vazio) e uma IoT Policy compartilhada de
  privilégio mínimo, escopada por dispositivo via
  `${iot:Connection.Thing.ThingName}`.
- **Status:** código pronto ✅ (`infrastructure/stacks/iot_stack.py`,
  `infrastructure/config/iot.py`, testes em `tests/unit/test_iot_stack.py`,
  `cdk synth` validado localmente). Nada implantado na AWS.
- **Critério de conclusão:** `cdk synth` local/CI passa sem credenciais
  AWS; testes semânticos da policy passam.
- **Dependências:** Fase 1A concluída.
- **Não inclui:** nenhum `AWS::IoT::Thing` individual, certificado X.509,
  chave privada, CSR, attachment, provisioning template ou IoT Rule
  (Basic Ingest) real — apenas os *nomes* das futuras regras estão
  reservados na configuração.

## Fase 1B.2 — arquitetura BLE-first

- **Escopo:** decisão arquitetural e documental para o onboarding
  BLE-first (BLE primário; QR e digitação manual do `setup_code` como
  fallback), nova terminologia (`setup_code`, `claim_session`, Fleet
  Provisioning temporary claim), e endurecimento da IoT Policy
  compartilhada da Fase 1B.1 com a condição
  `iot:Connection.Thing.IsAttached: true` em todas as statements.
- **Status:** código/docs prontos ✅ — `docs/adr/0001-ble-first-onboarding.md`,
  seção "Onboarding BLE-first" em `CONTEXT.md`, policy endurecida em
  `infrastructure/stacks/iot_stack.py`, testes atualizados. **"Pronto"
  aqui significa arquitetura registrada e policy endurecida — não BLE
  funcional.** Nenhum BLE, banco de dados, API, claim session real ou
  Fleet Provisioning foi implementado nesta fase.
- **Critério de conclusão:** ADR aceito, `CONTEXT.md` atualizado, policy
  endurecida testada, `cdk synth`/testes/lint passando sem credenciais
  AWS.
- **Dependências:** Fase 1B.1.
- **Não inclui:** nenhum recurso AWS novo (contagem funcional de recursos
  permanece igual à da Fase 1B.1); nenhuma alteração em `interBridge` ou
  `interapp`.

## Fase 1B.3 — bootstrap, diff e deploy mínimo

- **Escopo:** primeiro `cdk bootstrap` e `cdk deploy` autorizados para a
  `IoTStack` (Thing Type, Thing Group, IoT Policy endurecida).
- **Status:** concluída ✅ — em `dev`/`sa-east-1`:
  - `CDKToolkit`: `CREATE_COMPLETE`, bootstrap version 32.
  - `cdk diff` revisado manualmente antes do deploy.
  - `InterBridge-Dev-IoTStack`: `CREATE_COMPLETE` — `AWS::IoT::ThingType`
    `interbridge-dev-device`, `AWS::IoT::ThingGroup`
    `interbridge-dev-devices` (**ainda vazio**), `AWS::IoT::Policy`
    `interbridge-dev-device-policy` versão 1 (exatamente 1 policy, 4
    statements). Nenhum Thing individual, certificado, IoT Rule, Lambda,
    DynamoDB ou API foi criado.
  - CI atualizada de Node.js 20 para Node.js 22 no job que instala o CDK
    CLI.
- **Critério de conclusão:** deploy revisado via `cdk diff`, custo
  validado contra `docs/cost-controls.md`, recursos visíveis e tagueados
  corretamente na conta. **Atingido.**
- **Dependências:** Fase 1B.2 concluída; autorização explícita para
  bootstrap/deploy (obtida e executada).
- **A partir de agora:** qualquer mudança nova nesta ou em outra stack
  exige novo `cdk diff` revisado e nova autorização explícita antes de
  `cdk deploy` — ver `docs/deployment.md`.

## Fase 1C — DynamoDB Device Registry, Ownership e Claim Sessions

> **Nota de renumeração:** esta fase foi inserida entre a antiga "Fase 1C
> — primeiro dispositivo MQTT/mTLS" e a antiga "Fase 1D — Basic Ingest...".
> Essas duas foram renomeadas para **Fase 1D** e **Fase 1E**,
> respectivamente (ver abaixo), para abrir espaço para a camada de dados
> antes do primeiro dispositivo físico. Nenhuma decisão de escopo dessas
> duas fases mudou — só o número.

- **Escopo:** quatro tabelas DynamoDB (`Devices`, `SetupCodeLookups`,
  `DeviceMemberships`, `ClaimSessions`) na `DataStack`, e os modelos de
  domínio Python equivalentes (`domain/devices`, `domain/claims`,
  `domain/ownership`) — validação, enums, algoritmo de digest HMAC-SHA256
  do `setup_code`. Ver `docs/data-model.md` para o desenho completo.
- **Status:** implementada, testada e **implantada em DEV** ✅ — `cdk
  synth` sintetiza as quatro tabelas com sucesso; testes de infraestrutura
  (`tests/unit/test_data_stack.py`) e de domínio
  (`tests/unit/test_domain_*.py`) passando; `cdk diff` revisado; deploy
  de `InterBridge-Dev-DataStack` executado com sucesso em
  `dev`/`sa-east-1` em 2026-08-13 (`CREATE_COMPLETE`), sem alterar a
  `InterBridge-Dev-IoTStack`. Resultado implantado e **validado
  após o deploy** via AWS CLI:
  - quatro tabelas `ACTIVE` e vazias;
  - dois GSIs (`*-by-user-index` em `DeviceMemberships`,
    `*-by-device-index` em `ClaimSessions`);
  - TTL `ENABLED` no atributo `ttl` de `ClaimSessions`;
  - `deletion_protection` habilitada e `RemovalPolicy.RETAIN` nas quatro;
  - billing on-demand (`PAY_PER_REQUEST`) nas quatro.
  Nenhum registro de dispositivo, `setup_code`, membership ou claim
  session foi inserido — ver `docs/data-model.md`.
- **Critério de conclusão:** `cdk synth`/testes/lint passando sem
  credenciais AWS; nenhuma tabela sem retenção/proteção contra exclusão
  documentada; deploy revisado e validado. **Atingido.**
- **Dependências:** Fase 1B.3.
- **Não inclui:** Lambda, API Gateway, Cognito, Fleet Provisioning,
  certificados, Things individuais, IoT Rules, DynamoDB Streams, Secrets
  Manager, chave KMS gerenciada pelo cliente, dashboard. Nenhum dado real
  foi inserido nas tabelas.

## Fase 1D — primeiro dispositivo MQTT/mTLS

**Concluída no escopo definido ✅: primeiro dispositivo DEV controlado validado por MQTT/mTLS no
simulador e no ESP32-C3 real.** O mesmo Thing DEV e certificado individual do teste do simulador
foram usados na placa real; isso continua sendo provisionamento manual e controlado de bancada,
não Fleet Provisioning nem onboarding de produção.

- **Hardware de bancada validado:** ESP32-C3 Super Mini genérica, chip ESP32-C3, flash de 4 MB,
  USB-C com USB nativa, firmware compilado pelo PlatformIO no ambiente compatível com
  `esp32-c3-devkitm-1` e Wi-Fi 2,4 GHz. Essa placa não é necessariamente o módulo final da PCB
  comercial.
- **Fluxo validado:** build e upload USB; boot; conexão ao endpoint AWS IoT Data ATS na porta 8883
  com Amazon Root CA 1, certificado X.509 individual e chave correspondente mantida apenas
  localmente; `ClientId` igual/derivado do `device_id`; policy vinculada ao Thing; assinatura de
  comandos QoS 1; health inicial QoS 0; `OPEN_DOOR` publicado pela AWS CLI com JSON em Base64 no
  PowerShell; recebimento sem ação física; resposta segura e confirmação serial `response
  publish: ok`.
- **Reconexão validada:** desligamento completo, novo boot, reconexão ao Wi-Fi/AWS, recebimento de
  novo comando e publicação de nova resposta segura.
- **Observação operacional:** ocorreram falhas DNS transitórias. O endpoint foi validado
  externamente com registros A e AAAA, sem registrar hostname ou IP, e as retentativas existentes
  do firmware posteriormente estabeleceram a conexão.
- **Limite explícito:** queda e retorno do ponto de acesso enquanto o ESP32 permanece ligado **não
  foram testados** e continuam pendentes. Isso é diferente do boot frio/reconexão já validado.
- **Fora do escopo e pendente:** onboarding BLE real, Wi-Fi enviado pelo app, NVS e NVS criptografada,
  chave privada gerada no dispositivo, CSR, Fleet Provisioning, Secure Boot, Flash Encryption, OTA,
  hardware do interfone, GPIO/relé, fluxo de fabricação/produção e cleanup ou decisão formal de
  retenção do Thing DEV.


## Fase 1E — Basic Ingest, persistência real e observabilidade

- **Status:** concluída, implantada e validada em DEV/`sa-east-1` em 2026-08-18 ✅.
- **DataStack:** deploy concluído; a quinta tabela separada
  `interbridge-dev-telemetry` usa DynamoDB on-demand e TTL no atributo `expires_at` para registros
  temporários. As quatro tabelas da Fase 1C foram preservadas.
- **IngestionStack:** deploy concluído; Lambda `interbridge-dev-ingestion-telemetry-handler`, duas
  Topic Rules de Basic Ingest, quarentena sanitizada e DLQ técnica criadas. A reserved concurrency
  foi removida em DEV: o limite regional efetivo observado era 10, e reservar concorrência
  impediria manter o mínimo de 10 execuções não reservadas. Os aliases internos das rules são
  `ibmeta_device_id`, `ibmeta_category` e `ibmeta_received_at`, pois o AWS IoT SQL rejeitou aliases
  iniciados por underscore. Os guards `isUndefined(...)` são obrigatórios: `WHERE` é avaliado
  contra o payload original antes de `SELECT`, impedindo colisões com metadados internos.
- **ObservabilityStack:** deploy concluído com quatro alarmes CloudWatch: erros e throttles da
  Lambda, mensagens visíveis na quarentena e mensagens visíveis na DLQ técnica.
- **Validação ponta a ponta real:** um ESP32-C3 Super Mini confirmou MQTT/mTLS, assinatura de
  commands QoS 1, health QoS 0 persistido, recebimento de comandos publicados pelo AWS IoT MQTT
  Test Client e responses QoS 1 persistidas. Foram observados `STATE#CURRENT`, `METRIC#...` e
  `RESPONSE#...`, com `health_count=4`, `response_count=5` e `detailed_count=5` no período testado.
  As cinco respostas ficaram corretamente `REJECTED`/`COMMAND_EXPIRED`, porque os comandos
  `OPEN_DOOR` foram publicados depois da janela de validade de 10 segundos. Isso valida AWS IoT
  commands → ESP32 → responses → Basic Ingest → Lambda → DynamoDB, sem comprovar ações físicas: o
  smoke firmware as bloqueia propositalmente.
- **Ainda não validado:** payload inválido controlado na quarentena; falha controlada na DLQ
  técnica; transição real dos alarmes para `ALARM`; perda/recuperação do access point com a placa
  energizada; autenticação/API pública; BLE/Fleet Provisioning; ações físicas do interfone.
- **Próxima fase:** Fase 2 — autenticação e API base.

## Fase 2 — autenticação e API base

As subdivisões abaixo pertencem internamente à Fase 2; as Fases 3, 4 e 5 mantêm sua numeração.

### Fase 2A — arquitetura, ADRs e contratos

- **Status:** concluída documentalmente ✅; nenhum runtime ou deploy.
- **Escopo:** decisão Cognito User Pool/e-mail e senha, HTTP API com JWT Authorizer, identidade por
  `sub`, autorização por membership ativa, contrato futuro `/v1`, threat model e desenho do
  registro administrativo DEV. Ver `docs/adr/0003-phase-2-authentication-authorization.md`,
  `docs/phase-2-architecture.md` e `docs/openapi-v1.yaml`.
- **Critério de conclusão:** decisões, fluxos, matriz de papéis, antienumeração, OpenAPI validável,
  custos, ameaças e pendências revisados; nenhuma alteração em infraestrutura/runtime/AWS.
- **Dependências:** Fase 1E concluída, implantada e validada em DEV.

### Fase 2B — backend de autenticação, API base e registro administrativo DEV

- **Escopo:** implementar, em revisão separada, Cognito, HTTP API/authorizer, consultas base, a
  operação interna controlada que registra legitimamente o dispositivo DEV e OWNER ativo, e o
  gerenciamento básico do dispositivo pelo próprio usuário (`PATCH` do `display_name`, um nome
  amigável opcional por dispositivo -- nunca um campo de cômodo/ambiente, restrito a `OWNER`).
- **Critério de conclusão:** infraestrutura/runtime testados, segurança/IAM/custos revisados,
  registro idempotente auditável e deploy DEV autorizado/validado.
- **Não inclui:** integração do app, comando físico, social login, MFA/SMS ou produção.

### Fase 2C — integração inicial do interapp

- **Escopo:** login e consultas de dispositivo/status pelo app via HTTPS.
- **Critério de conclusão:** app autentica e consulta somente dispositivos autorizados em DEV,
  incluindo erros e recuperação de sessão.
- **Dependências:** Fase 2B.

### Fase 2D — comandos assíncronos pela API

- **Escopo:** implementar emissão permitida, idempotência/rate limiting e consulta de resultado,
  preservando o protocolo oficial e sem confundir publish com execução.
- **Critério de conclusão:** fluxo `202` → polling → estado terminal/expirado testado, com isolamento
  por membership/dispositivo e falhas assíncronas cobertas.
- **Dependências:** Fases 2B–2C.

### Fase 2E — validação ponta a ponta

- **Escopo:** validar app → API → MQTT → ESP32 → resposta persistida → API/app em DEV.
- **Critério de conclusão:** evidências sanitizadas de autenticação, autorização, comando e resposta,
  sem afirmar ação física além do que for explicitamente testado.
- **Dependências:** Fase 2D.

## Fase 3 — claim sessions (API), BLE-first e Fleet Provisioning

- **Escopo:** implementação real do fluxo descrito em
  `docs/adr/0001-ble-first-onboarding.md` sobre a camada de dados já
  criada na Fase 1C — os quatro endpoints `/devices/claim/*` (Lambda +
  API Gateway), a transação atômica de conclusão do claim (ver
  `docs/data-model.md`), integração com AWS IoT Fleet Provisioning by
  Trusted User, verificação cloud-side de conclusão, e proteção contra
  abuso (rate limiting). O pepper do HMAC de `setup_code` é provisionado
  nesta fase (ver `docs/data-model.md`).
- **Critério de conclusão:** um dispositivo novo pode ser reivindicado por
  um usuário de ponta a ponta (BLE primário, QR/manual como fallback), com
  certificado emitido de forma segura e propriedade confirmada
  cloud-side.
- **Dependências:** Fase 2; decisão sobre processo seguro de emissão de
  certificados (ver "Pendências" em `CONTEXT.md`). O schema de Device
  Registry/Claim Session já está fechado desde a Fase 1C.

## Fase 4 — integração completa do interapp

- **Escopo:** todas as telas do `interapp` funcionais contra o backend
  real (sem mocks), incluindo envio de comandos e recebimento de eventos.
- **Critério de conclusão:** uso ponta a ponta funcional com pelo menos um
  dispositivo real.
- **Dependências:** Fase 3.

## Fase 5 — OTA, Jobs, escala e produção

- **Escopo:** atualizações OTA via AWS IoT Jobs, provisionamento em
  escala, estratégia DEV/PROD, revisão de custos em escala de produção,
  permissões de privilégio mínimo para Device Shadow/IoT Jobs (ver
  `docs/adr/0001-ble-first-onboarding.md`).
- **Critério de conclusão:** processo repetível de fabricação/provisioning
  de novos dispositivos, OTA testado com rollback.
- **Dependências:** Fase 4; decisões abertas sobre separação de contas e
  identidade comercial (ver `CONTEXT.md`).
# Estado da Fase 2B

Infraestrutura implantada em DEV, com validação ponta a ponta ainda pendente. O primeiro PATCH
falhou no cold start por pacote incompleto e nenhum `display_name` foi escrito. Seis rotas estão
roteadas: as três leituras, `PATCH /v1/devices/{device_id}`
(`display_name`) e as duas rotas de comando da Fase 2D abaixo.

## Gerenciamento de dispositivos — hotfix pós-deploy pendente

Primeira evolução do gerenciamento de dispositivos: `GET /v1/devices` (lista por membership ativa),
`GET /v1/devices/{device_id}` (detalhe, agora incluindo `created_at`/`updated_at` quando presentes)
e `PATCH /v1/devices/{device_id}` para definir ou limpar o `display_name` da membership.
Deliberadamente **não** há campo de cômodo/ambiente -- o produto modela um InterBridge por
residência. Cada usuário pode ver um nome diferente para o mesmo InterBridge; qualquer membership
`ACTIVE` (`OWNER`, `ADMIN` ou `MEMBER`) altera apenas o próprio apelido. `display_name` nunca
autoriza, nunca é chave/tópico/identidade, e o rótulo
de fallback ("InterBridge") é responsabilidade do app, nunca persistido pelo backend. Ver
`domain/ownership/display_name.py`, `lambdas/device_api/handler.py` e
`docs/phase-2-architecture.md`. O hotfix torna o handler autocontido no asset `lambdas` e corrige
os placeholders do DynamoDB; ainda precisa de deploy manual e reteste pelo app Android.

## Fase 2D — comandos assíncronos autenticados (implementada localmente)

Fase 2C concluída e validada em DEV. POST de criação OWNER-only e GET de estado para memberships
ativas OWNER/ADMIN/MEMBER foram implementados com intenção antes do publish, idempotência e cooldown
atômicos e mapeamento conservador de resposta. Ainda não houve deploy, publish MQTT ou ação física.
Ver `docs/phase-2d-runbook.md`.
