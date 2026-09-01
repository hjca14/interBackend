# Ciclo de vida do toque/chamada

O pipeline permanece único: firmware publica em `interbridge/{device_id}/events`, a ingestão
valida e persiste, e invoca assincronamente o sender existente. O sender deduplica por
`(device_id, event_id)`, resolve membros e instalações autorizados e usa o mesmo caminho de
retry, limpeza de token inválido e DLQ.

`event_id` identifica uma mensagem; `call_id` identifica a sessão. Início e término têm
`event_id` diferentes e o mesmo `call_id`, no formato `call-` mais 32 hexadecimais minúsculos.
`RING_DETECTED` legado sem `call_id` continua aceito: o backend deriva deterministicamente
`call-<sufixo do event_id>`. Como o firmware antigo não pode publicar um término correlacionado,
esse início depende do timeout de 30 segundos no app. `RING_ENDED` sem `call_id` é inválido e vai
para a quarentena existente.

Exemplos MQTT (o prefixo `ibmeta_*` é acrescentado pela regra IoT, não pelo firmware):

```json
{"protocol_version":1,"device_id":"ib-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","event_id":"evt-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","call_id":"call-cccccccccccccccccccccccccccccccc","event":"RING_DETECTED","timestamp":"2026-09-01T15:00:00Z"}
```

```json
{"protocol_version":1,"device_id":"ib-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","event_id":"evt-dddddddddddddddddddddddddddddddd","call_id":"call-cccccccccccccccccccccccccccccccc","event":"RING_ENDED","timestamp":"2026-09-01T15:00:08Z"}
```

Os modos públicos são exclusivos: `RING_ONLY` abre a experiência de chamada,
`NOTIFICATION_ONLY` produz apenas a notificação comum e `NONE` não envia início. O valor legado
`RING_AND_NOTIFICATION` ainda é desserializado, mas é normalizado para `RING_ONLY`, sem uma
segunda notificação redundante. Em horário de silêncio, `NOTIFICATION_ONLY` não cria uma
capacidade que o modo base não possuía; `BLOCK_ALL` bloqueia todos os alertas.

O início e o término usam mensagens FCM data-only, prioridade Android `high` e TTL de 30
segundos para o início e 60 segundos para o término. Antes de consultar instalações ou FCM, o
sender compara o relógio UTC injetável ao `occurred_at` original: `RING_DETECTED` é suprimido no
limite exato de 30 segundos e `RING_ENDED` no limite exato de 60 segundos. Skew futuro de até 5
segundos é tolerado; tempo mais futuro é suprimido. O TTL enviado ao FCM é somente o restante da
janela lógica, enquanto `expires_at` continua representando o deadline absoluto.

O timestamp MQTT é o horário original (`timestamp_source=device`). Quando ausente, a ingestão
continua usando `received_at` para ordenar/persistir o histórico, mas marca
`timestamp_source=unknown`; ele não é apresentado internamente como horário original comprovado e
o push falha fechado. Eventos atrasados, futuros inválidos ou de horário desconhecido continuam
persistidos antes da invocação do sender. A push delivery termina como `COMPLETED`, com `outcome`
`SUPPRESSED_EXPIRED`, `SUPPRESSED_UNKNOWN_EVENT_TIME` ou `SUPPRESSED_FUTURE_EVENT_TIME`, contador
correspondente e `sent_count=0`. O retry encontra essa decisão terminal e não passa a enviar depois.

O término não contém `notification`, som ou `presentation_intent`; ele instrui o app a
cancelar localmente somente a sessão com o mesmo `call_id`. Assim, duplicação, inversão ou um
término atrasado de X não cancelam uma chamada Y. O backend o envia às instalações ativas dos
membros atualmente autorizados ao mesmo dispositivo, mesmo se a preferência atual estiver
`NONE`: a correlação exata torna o comando inofensivo quando a sessão não existe e evita deixar
uma chamada já apresentada ativa após mudança de preferência.

FCM/Android não garantem entrega após force-stop, revogação de notificações, canal desativado,
DND ou restrições do sistema. O timeout local continua sendo o fallback. GPIO4 e GPIO3 são apenas
simuladores DEV temporários no firmware; o backend não depende do Si3050. A detecção física real
do fim do ringing ainda não foi validada. Esta mudança não implanta infraestrutura.
