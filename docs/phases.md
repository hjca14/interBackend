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
- **Status:** pendente ⏳ — bootstrap, diff contra a conta real e deploy
  não executados.
- **Critério de conclusão:** deploy revisado via `cdk diff`, custo
  validado contra `docs/cost-controls.md`, recursos visíveis e tagueados
  corretamente na conta.
- **Dependências:** Fase 1B.2 concluída; autorização explícita para
  bootstrap/deploy (ainda não obtida).

## Fase 1C — primeiro dispositivo MQTT/mTLS

- **Escopo:** provisionamento manual/controlado de um primeiro certificado
  X.509 de teste (fora do repositório), conexão de um dispositivo real ou
  simulado via MQTT/TLS mútuo ao AWS IoT Core.
- **Critério de conclusão:** dispositivo de teste publica/recebe mensagens
  seguindo `interBridge/docs/communication-protocol.md`.
- **Dependências:** Fase 1B.3; protocolo v1 estável no `interBridge`.
- **Pendente:** decisão em `docs/adr/0001-ble-first-onboarding.md` sobre
  o momento de migrar para associação exclusiva (`EXCLUSIVE_THING`).

## Fase 1D — Basic Ingest, persistência e observabilidade

- **Escopo:** regras de Basic Ingest (usando os nomes já reservados em
  `infrastructure/config/iot.py`), Lambdas de processamento, modelo de
  dados DynamoDB fechado e implementado, dashboard/alarmes mínimos na
  `ObservabilityStack`.
- **Critério de conclusão:** eventos de um dispositivo de teste são
  persistidos e visíveis via consulta direta ao DynamoDB (ainda sem API
  pública).
- **Dependências:** Fase 1C.

## Fase 2 — autenticação e API base

- **Escopo:** mecanismo de autenticação para usuários do `interapp`,
  endpoints reais na `ApiStack` (listar dispositivos, status, comandos).
- **Critério de conclusão:** `interapp` consegue autenticar e consultar
  status de um dispositivo de teste via HTTPS.
- **Dependências:** Fase 1D.

## Fase 3 — claim sessions, BLE-first e Fleet Provisioning

- **Escopo:** implementação real do fluxo descrito em
  `docs/adr/0001-ble-first-onboarding.md` — Device Registry, Claim
  Session, os quatro endpoints `/devices/claim/*`, integração com AWS IoT
  Fleet Provisioning by Trusted User, verificação cloud-side de conclusão,
  e proteção contra abuso (rate limiting).
- **Critério de conclusão:** um dispositivo novo pode ser reivindicado por
  um usuário de ponta a ponta (BLE primário, QR/manual como fallback), com
  certificado emitido de forma segura e propriedade confirmada
  cloud-side.
- **Dependências:** Fase 2; decisão sobre processo seguro de emissão de
  certificados e sobre o schema definitivo de Device Registry/Claim
  Session (ver "Pendências" em `CONTEXT.md`).

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
