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

* único comando remoto aprovado: `OPEN_DOOR`, sempre com `parameters: {}`; `RESTART` não é exposto
  sem caso de uso e política aprovados;
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

Basic Ingest preserva cada `RESPONSE#...` histórico e mantém idempotentemente a projeção
`COMMAND_RESULT#<command_id>`. O GET lê intenção e projeção por `GetItem` fortemente consistente,
em O(1), sem Scan/Query do histórico. `ACCEPTED` não substitui terminal; entre terminais vence o
mais novo pelo timestamp confiável de recebimento do IoT Rule.

## Capacidade semântica OPEN_DOOR

`OPEN_DOOR` nunca transporta tecla, sequência DTMF, GPIO, duração de pulso ou modo de acionamento.
A configuração física futura pertence ao `Device`, nunca à membership, e somente OWNER poderá
alterá-la futuramente:

* `DISABLED`: padrão seguro;
* `DTMF`: sequência configurada localmente no dispositivo;
* `RELAY`: futuro, com pulso local limitado.

Nada disso é implementado neste PR. O firmware segue como autoridade final e rejeita
`OPEN_DOOR` com `NOT_CONFIGURED`/`CAPABILITY_DISABLED` quando desabilitado ou não configurado. O
backend sanitiza essa rejeição; publish, `ACCEPTED` e ausência de resposta nunca significam portão
aberto.

## Endpoint AWS IoT Data Plane

Em cold start, a Lambda chama uma vez `DescribeEndpoint(endpointType="iot:Data-ATS")`, valida o
hostname retornado e cria `iot-data` com `endpoint_url=https://<Data-ATS>`. Os clientes ficam em
cache para invocações warm: não há hardcode, Account ID ou endpoint real no repositório e não há
lookup por request. AWS IoT não oferece escopo de recurso para `iot:DescribeEndpoint`, portanto a
policy usa somente essa ação com `Resource: "*"`; `iot:Publish` permanece em ARN de tópico restrito.

## Ordem futura e validação controlada

1. revisar os templates sintetizados e IAM offline;
2. executar `cdk diff` autorizado de DataStack, IngestionStack e ApiStack;
3. confirmar que DataStack não substitui nem altera tabelas;
4. implantar IngestionStack primeiro, pois ela mantém a projeção
   `COMMAND_RESULT#<command_id>` consumida pelo GET;
5. validar que a ingestão existente de health, events, histórico de responses, métricas e projeção
   continua funcionando;
6. implantar ApiStack somente depois;
7. validar POST/GET de comandos inicialmente com rejeição segura, sem ação física, confirmando
   intenção, publish, resposta Basic Ingest e estados sem interpretar publish como execução.

Implantar ApiStack antes de IngestionStack pode expor o GET enquanto a ingestão ainda não mantém
`COMMAND_RESULT#<command_id>`; nesse intervalo, uma resposta MQTT histórica pode existir e o GET não
encontrar sua projeção direta.

Nada dessa ordem foi executado neste PR.

## Rollback

Reverter as duas rotas, Lambdas e permissões específicas pelo CDK. Não apagar, esvaziar nem
substituir tabelas; não apagar User Pool. Itens com TTL expiram naturalmente. Se houver incidente,
desabilitar primeiro o POST/API e retirar `iot:Publish` do criador; preservar dados para diagnóstico
sanitizado.

## Decisões adiadas e custo

Custos qualitativos adicionais: duas Lambdas/API integrations, reads/writes on-demand (transação
custa mais que Put simples), projeção direta de resposta, um lookup de endpoint por cold start,
logs e mensagens IoT. Alarmes/SLOs, limites de produção e catálogo futuro estão adiados. Nome
personalizado por usuário permanece backlog futuro em `DeviceMembership`.
