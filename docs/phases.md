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

- **Escopo:** provisionamento manual/controlado de um primeiro certificado
  X.509 de teste (fora do repositório), conexão de um dispositivo real ou
  simulado via MQTT/TLS mútuo ao AWS IoT Core.
- **Critério de conclusão:** dispositivo de teste publica/recebe mensagens
  seguindo `interBridge/docs/communication-protocol.md`.
- **Dependências:** Fase 1C; protocolo v1 estável no `interBridge`.
- **Fase 1D.1:** simulador seguro e runbook preparados localmente em
  `mqtt_smoke/` e `docs/mqtt-smoke-test.md`. **Não implantado e ainda não
  validado na nuvem**; nenhum Thing/certificado foi criado e a Fase 1D não
  está concluída.
- **Pendente:** decisão em `docs/adr/0001-ble-first-onboarding.md` sobre
  o momento de migrar para associação exclusiva (`EXCLUSIVE_THING`).

## Fase 1E — Basic Ingest, persistência real e observabilidade

- **Escopo:** regras de Basic Ingest (usando os nomes já reservados em
  `infrastructure/config/iot.py`), Lambdas de processamento que escrevem
  nas tabelas da Fase 1C, dashboard/alarmes mínimos na
  `ObservabilityStack`.
- **Critério de conclusão:** eventos de um dispositivo de teste são
  persistidos (nas tabelas já criadas na Fase 1C) e visíveis via consulta
  direta ao DynamoDB (ainda sem API pública).
- **Dependências:** Fase 1D; modelo de dados da Fase 1C (já fechado).

## Fase 2 — autenticação e API base

- **Escopo:** mecanismo de autenticação para usuários do `interapp`,
  endpoints reais na `ApiStack` (listar dispositivos, status, comandos),
  consumindo as tabelas da Fase 1C.
- **Critério de conclusão:** `interapp` consegue autenticar e consultar
  status de um dispositivo de teste via HTTPS.
- **Dependências:** Fase 1E.

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
