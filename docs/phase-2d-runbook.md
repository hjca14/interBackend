# Runbook — Fase 2D: comandos assíncronos autenticados

## Estado e limites

A Fase 2C foi concluída e validada em DEV. A Fase 2D está implementada e validada apenas
localmente: ainda não foi implantada. Durante este trabalho nenhum comando foi publicado, nenhuma
ação física foi testada, nenhuma chamada AWS foi feita e nenhuma tabela recebeu escrita.

As únicas rotas novas são `POST /v1/devices/{device_id}/commands` e
`GET /v1/devices/{device_id}/commands/{command_id}`. `202` confirma persistência e publish aceito
pelo SDK, não recebimento pelo dispositivo, execução nem ação física. Somente uma resposta terminal
persistida pelo Basic Ingest produz `COMPLETED` ou `REJECTED`.

## Contrato operacional DEV

* comandos remotos: `OPEN_DOOR` e `RESTART`, sempre com `parameters: {}`;
* validade do comando: 30 segundos; intenção retida por TTL por 30 dias;
* idempotência opcional: 24 horas, escopo `sub + device_id + corpo canônico`; somente digests
  SHA-256 da chave/escopo e do corpo são persistidos;
* cooldown atômico: 2 segundos por usuário/dispositivo, com `Retry-After: 2`;
* throttle do POST: 1 requisição/segundo, burst 2, no stage DEV;
* MQTT: `interbridge/{device_id}/commands`, QoS 1, `retain=false`, máximo 8 KiB.

Esses números são controles pequenos de DEV, não SLOs de produção. Antes de produção devem ser
revistos com métricas de uso, latência do dispositivo, comportamento de retry e orçamento.

## Itens adicionais na Telemetry

A tabela existente continua com PK `device_id`, SK `record_key` e TTL `expires_at`; nenhuma chave,
GSI ou recurso físico muda, portanto não há replacement:

| `record_key` | Conteúdo | TTL/consistência |
| --- | --- | --- |
| `COMMAND#<command_id>` | intenção, comando e epochs emitido/expiração | 30 dias; leitura forte |
| `IDEMPOTENCY#<sha256>` | `command_id` e digest do corpo | 24 horas; leitura forte |
| `COOLDOWN#<sha256>` | próximo epoch permitido | 2 segundos; condição transacional |

Criação usa uma única `TransactWriteItems`: intenção, marcador opcional e cooldown são `Put`
condicionais. Não existe check-then-write. Persistência antecede publish. Retry idempotente reutiliza
e pode republicar o mesmo `command_id`, inclusive após falha ambígua; a deduplicação permanece
responsabilidade do protocolo/firmware. TTL é limpeza assíncrona, nunca decisão de validade.

O GET lê intenção fortemente e consulta as respostas `RESPONSE#...` fortemente. Isso preserva as
chaves atuais, mas custa uma Query de respostas do dispositivo por consulta; o acesso deve ser
reavaliado antes de volume de produção, sem alterar o contrato público.

## Ordem futura e validação controlada

1. revisar o template sintetizado e IAM offline;
2. executar `cdk diff` autorizado e revisar que tabelas/User Pool não são substituídos;
3. implantar Data (sem mudança física), Api e depois validar as cinco rotas;
4. testar primeiro rejeição segura no simulador; só depois considerar hardware controlado;
5. confirmar intenção, publish, resposta Basic Ingest e estados, sem interpretar publish como ação.

Nada dessa ordem foi executado neste PR.

## Rollback

Reverter as duas rotas, Lambdas e permissões específicas pelo CDK. Não apagar, esvaziar nem
substituir tabelas; não apagar User Pool. Itens com TTL expiram naturalmente. Se houver incidente,
desabilitar primeiro o POST/API e retirar `iot:Publish` do criador; preservar dados para diagnóstico
sanitizado.

## Decisões adiadas e custo

Custos qualitativos adicionais: duas Lambdas/API integrations, reads/writes on-demand (transação
custa mais que Put simples), Query de respostas, logs e mensagens IoT. Alarmes/SLOs, projeção de
resposta por chave direta, limites de produção e catálogo futuro estão adiados. Nome personalizado
por usuário permanece backlog futuro em `DeviceMembership`.
