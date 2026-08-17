# ADR 0002 — Basic Ingest e tabela operacional de telemetria

**Status:** Accepted for local implementation; deployment pending

## Decisão

Persistir telemetria em uma única tabela operacional pertencente ao DataStack, sem misturar
histórico ao registry/claim da Fase 1C. Um IngestionStack separado possui as duas regras Basic
Ingest e runtime; ObservabilityStack apenas referencia métricas do runtime. Isso mantém ownership
claro e evita circularidade.

O teto detalhado é reservado atomicamente junto ao detalhe. Duplicatas são identificadas pela SK
que contém `event_id` (`evt-<32 hex minúsculos>`)/`command_id`; health e conectividade técnica somente agregam. Payloads são
fail-closed, limitados a 8 KiB e normalizados por allowlist. Inválidos geram somente envelope
sanitizado na quarentena; falhas de infraestrutura propagam para retry/DLQ/error action.

## Consequências

Transações de dois itens custam mais, mas dão um limite correto sob concorrência. A consulta de
estado, eventos, respostas e métricas exige quatro queries separadas por prefixo. Não há acesso
pelas quatro tabelas da Fase 1C, Scan, GSI, stream, dashboard ou métricas por dispositivo.

## Filas de falha

`InvalidMessageQuarantine` recebe apenas metadados sanitizados enviados explicitamente pelo
handler. `IngestionTechnicalDlq` recebe a Lambda DLQ e o errorAction das rules e pode conter o evento
original; acesso e inspeção devem tratá-la como sensível. As duas usam criptografia SQS-managed e
retenção DEV curta.
