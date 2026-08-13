# Fases planejadas

Este documento registra as fases planejadas para o `interBackend`, seus
critérios de conclusão e dependências. Ver também `CONTEXT.md` para o
estado atual detalhado.

## Fase 1A — fundação do backend e CDK (esta tarefa)

- **Escopo:** estrutura do projeto CDK v2 em Python, configuração tipada de
  ambiente/região/tags, quatro stacks preparatórias (sem recursos reais ou
  com recursos estritamente seguros), testes, CI local reprodutível,
  documentação, proteção contra segredos.
- **Critério de conclusão:** `ruff check`, `ruff format --check`, `mypy`,
  `pytest` e `cdk synth` executam com sucesso localmente e em CI, sem
  acessar a conta AWS.
- **Dependências:** nenhuma (ponto de partida).
- **Não inclui:** `cdk bootstrap`, `cdk deploy`, qualquer recurso AWS real.

## Fase 1B — infraestrutura mínima AWS

- **Escopo:** primeiro `cdk bootstrap` e `cdk deploy` autorizados, com o
  menor conjunto de recursos possível (provavelmente uma tabela DynamoDB
  mínima e/ou o esqueleto de AWS IoT Core).
- **Critério de conclusão:** deploy revisado via `cdk diff`, custo validado
  contra `docs/cost-controls.md`, recursos visíveis e tagueados
  corretamente na conta.
- **Dependências:** Fase 1A concluída; autorização explícita para deploy.

## Fase 1C — primeiro dispositivo MQTT/TLS

- **Escopo:** provisionamento manual/controlado de um primeiro certificado
  X.509 de teste (fora do repositório), conexão de um dispositivo real ou
  simulado via MQTT/TLS ao AWS IoT Core.
- **Critério de conclusão:** dispositivo de teste publica/recebe mensagens
  seguindo `interBridge/docs/communication-protocol.md`.
- **Dependências:** Fase 1B; protocolo v1 estável no `interBridge`.

## Fase 1D — ingestão, persistência e observabilidade

- **Escopo:** regras de Basic Ingest, Lambdas de processamento, modelo de
  dados DynamoDB fechado e implementado, dashboard/alarmes mínimos na
  `ObservabilityStack`.
- **Critério de conclusão:** eventos de um dispositivo de teste são
  persistidos e visíveis via consulta direta ao DynamoDB (ainda sem API
  pública).
- **Dependências:** Fase 1C.

## Fase 2 — autenticação do usuário e API do app

- **Escopo:** mecanismo de autenticação para usuários do `interapp`,
  endpoints reais na `ApiStack` (listar dispositivos, status, comandos).
- **Critério de conclusão:** `interapp` consegue autenticar e consultar
  status de um dispositivo de teste via HTTPS.
- **Dependências:** Fase 1D.

## Fase 3 — claim por QR e provisioning

- **Escopo:** fluxo de reivindicação de dispositivo via QR
  (`interbridge://claim?...`), Fleet Provisioning real.
- **Critério de conclusão:** um dispositivo novo pode ser reivindicado por
  um usuário de ponta a ponta, com certificado emitido de forma segura.
- **Dependências:** Fase 2; decisão sobre processo seguro de emissão de
  certificados (ver "Pendências" em `CONTEXT.md`).

## Fase 4 — integração completa do interapp

- **Escopo:** todas as telas do `interapp` funcionais contra o backend
  real (sem mocks), incluindo envio de comandos e recebimento de eventos.
- **Critério de conclusão:** uso ponta a ponta funcional com pelo menos um
  dispositivo real.
- **Dependências:** Fase 3.

## Fase 5 — fleet provisioning, OTA e produção

- **Escopo:** provisionamento em escala, atualizações OTA via AWS IoT
  Jobs, estratégia DEV/PROD, revisão de custos em escala de produção.
- **Critério de conclusão:** processo repetível de fabricação/provisioning
  de novos dispositivos, OTA testado com rollback.
- **Dependências:** Fase 4; decisões abertas sobre separação de contas e
  identidade comercial (ver `CONTEXT.md`).
