# Sender FCM e aplicação de preferências (Fase 3B.6/3B.7)

**Estado atual: implementado, testado e implantado em DEV; validado ponta a ponta com evento
sintético e, depois, com evento originado em ESP32 real.** O fluxo real percorreu AWS IoT,
`telemetry_ingestion`, `push_sender`, FCM e o app Android. Ver "Validação e limitações" no final
deste documento. O CDK apenas referencia a credencial Firebase; não a cria.

Esta entrega cobre, em um único PR, as Fases **3B.6** (sender FCM) e **3B.7** (aplicação das
preferências e quiet mode), porque o sender só é significativo já filtrando corretamente por
preferência -- um sender que ignorasse `notification_preferences` não seria um incremento útil
por si só. As duas fases permanecem rastreáveis separadamente no roadmap (`docs/phases.md`).

## Visão geral do fluxo

```
firmware --MQTT/mTLS--> AWS IoT Basic Ingest (regra já existente da Fase 1E)
                              |
                              v
                lambdas/telemetry_ingestion (inalterado: continua
                persistindo em Telemetry) -- após persistir um evento
                RING_DETECTED, invoca de forma assíncrona e best-effort
                (Lambda InvocationType=Event) o push sender
                              |
                              v
                lambdas/push_sender (novo):
                  1. valida o invocation payload (event.py)
                  2. reivindica idempotência atômica (idempotency.py)
                  3. consulta memberships ativos do device (memberships.py)
                  4. avalia preferências por membership (domain/push/preferences.py)
                  5. consulta instalações push ativas dos destinatários (installations.py)
                  6. compõe o payload FCM (domain/push/payload.py)
                  7. obtém/renova o access token Firebase (firebase_auth.py)
                  8. envia via FCM HTTP v1 (fcm_client.py)
                  9. classifica o resultado (domain/push/fcm_result.py)
                 10. remove token definitivamente inválido, se for o caso (cleanup.py)
                 11. registra logs/métricas (metrics.py) e conclui a idempotência
```

## 1. Por que reutilizar o caminho de ingestão existente

O contrato de entrada (`protocol_version`, `device_id`, `event`, `event_id`, `timestamp`
opcional) **já existe e já está implementado** em `domain/telemetry/models.py` desde a Fase 1E --
`RING_DETECTED` já é um dos valores aceitos em `EVENTS`, `event_id` já segue o formato
`evt-<32 hex minúsculos>`, e a validação de tamanho/formato, a rejeição de tipos de evento não
suportados e a desconfiança de qualquer campo além do evento em si (nunca `user_id`, preferências,
tokens ou destinatários) já são exatamente o que esta fase precisava. Não havia necessidade -- nem
permissão, dado o objetivo de não criar protocolo paralelo -- de reimplementar isso.

A regra Basic Ingest (`AWS::IoT::TopicRule`, Fase 1E) já existe e já invoca
`lambdas/telemetry_ingestion`. Uma restrição do AWS IoT Basic Ingest torna essa reutilização a
**única** forma de reagir ao mesmo evento sem alterar o firmware: o dispositivo publica
diretamente em `$aws/rules/{nome-da-regra}/interbridge/{device_id}/events` -- esse prefixo
especial invoca *apenas* a regra nomeada e a mensagem nunca chega ao tópico "simples"
`interbridge/{device_id}/events` (ver `infrastructure/config/iot.py`). Uma segunda
`AWS::IoT::TopicRule` independente, inscrita no tópico simples, simplesmente nunca veria a
mensagem. Portanto, o único jeito de reagir ao mesmo evento sem pedir ao firmware que publique em
um segundo prefixo `$aws/rules/...` (mudança de firmware, fora do escopo desta entrega) é a partir
de dentro da própria invocação já existente.

`lambdas/telemetry_ingestion/handler.py` ganhou por isso uma única responsabilidade nova, depois
de persistir a telemetria (inalterada): quando `category == "events"` e `event == "RING_DETECTED"`,
invoca **de forma assíncrona e best-effort** (`lambda:InvokeFunction`, `InvocationType="Event"`) o
Lambda `push_sender`, passando um envelope interno mínimo e já validado:

```json
{"schema_version": 1, "device_id": "ib-...", "event_id": "evt-...", "event": "RING_DETECTED", "occurred_at": "2026-08-20T12:00:00Z"}
```

Esse envelope **não é um novo protocolo de dispositivo** -- é apenas a passagem de parâmetros já
validados de uma função Lambda para outra dentro do próprio backend. `push_sender` ainda assim
revalida essa estrutura (`lambdas/push_sender/event.py`), usando os mesmos padrões canônicos de
`device_id`/`event_id` de `domain/telemetry/models.py`, como defesa em profundidade contra um
chamador malformado -- não como uma segunda definição do contrato.

Uma falha ao *iniciar* essa invocação (ex.: IAM mal configurado) é registrada e engolida:
a persistência da telemetria, que já terminou com sucesso, nunca é desfeita ou reprocessada só
porque a notificação falhou ao ser disparada.

### Por que a invocação assíncrona é o gatilho certo para "a entrega pode ocorrer mais de uma vez"

Uma invocação assíncrona do Lambda (`InvocationType="Event"`) -- seja ela iniciada por um serviço
AWS ou, como aqui, por outro Lambda -- entra no mesmo subsistema de invocação assíncrona da AWS,
que automaticamente tenta novamente em caso de falha/throttle (até `retry_attempts`, configurado
como 2 nesta entrega) antes de rotear para o destino de falha configurado (uma fila SQS dedicada,
`InterBridge-Dev-NotificationStack`). Esse é exatamente o motivo pelo qual `push_sender` precisa da
sua própria idempotência autoritativa, independente da deduplicação interna do
`telemetry_ingestion` (ver seção 3).

## 2. Separação de componentes

| Componente | Arquivo | Toca AWS/rede? |
| --- | --- | --- |
| Validação do evento (envelope interno) | `lambdas/push_sender/event.py` | Não |
| Idempotência | `lambdas/push_sender/idempotency.py` | Sim (DynamoDB) |
| Memberships ativos | `lambdas/push_sender/memberships.py` | Sim (DynamoDB) |
| Avaliação de preferências | `domain/push/preferences.py` | Não (puro) |
| Instalações push | `lambdas/push_sender/installations.py` | Sim (DynamoDB) |
| Composição da mensagem | `domain/push/payload.py` | Não (puro) |
| Credencial/token Firebase | `lambdas/push_sender/firebase_auth.py` | Sim (Secrets Manager + Google) |
| Cliente FCM | `lambdas/push_sender/fcm_client.py` | Sim (HTTPS) |
| Classificação do resultado | `domain/push/fcm_result.py` | Não (puro) |
| Limpeza de token inválido | `lambdas/push_sender/cleanup.py` | Sim (DynamoDB) |
| Logs/métricas | `lambdas/push_sender/metrics.py` | Sim (CloudWatch Logs, via EMF) |
| Orquestração | `lambdas/push_sender/handler.py` | Chama os componentes acima |

`lambdas/push_sender/handler.py` **apenas orquestra**: ele não contém lógica de decisão de
preferências, não monta payload FCM, não classifica respostas HTTP -- cada uma dessas
responsabilidades vive em seu próprio módulo, puro sempre que possível.

## 3. Idempotência

**Esta seção foi corrigida duas vezes após revisão de CI/design; o texto abaixo descreve a
implementação final.** A primeira versão só recuperava um crash via o TTL de 2 horas do item
(neutralizava as repetições assíncronas do próprio Lambda). A segunda versão trocou isso por uma
concessão (lease) de 90 segundos, mas ainda tinha uma lacuna real: um retry que chegasse *durante*
a lease (o que a AWS não garante que não aconteça) recebia `DUPLICATE_IN_FLIGHT` e a invocação
terminava como "sucesso" do ponto de vista da AWS -- sem garantia de que um *outro* retry chegaria
depois da lease expirar. A versão atual resolve isso com uma operação explícita de **abandono**:
uma falha *reconhecida* como recuperável libera a concessão imediatamente, em vez de confiar em
temporização.

### Tabela dedicada

`interbridge-dev-push-notification-deliveries` (nova, `infrastructure/stacks/data_stack.py`):

| Atributo | Tipo | Papel |
| --- | --- | --- |
| `device_id` | String (PK) | |
| `event_id` | String (SK) | |
| `status` | String | `PROCESSING` ou `COMPLETED` (nunca um terceiro valor -- ver `abandon()` abaixo) |
| `attempt` | Number | incrementado a cada aquisição/retomada da concessão |
| `claimed_at` | Number | epoch segundos da primeira tentativa |
| `lease_expires_at` | Number | epoch segundos em que a concessão atual expira -- **não** é o TTL do item |
| `updated_at` | Number | epoch segundos da última escrita |
| `expires_at` | Number (TTL) | `claimed_at + 7200` (2 horas) -- só limpeza, não decide recuperação |
| `membership_count`, `installation_count`, `sent_count`, `suppressed_count`,
  `invalid_token_count`, `temporary_failure_count`, `permanent_failure_count`,
  `auth_config_failure_count` | Number | gravados apenas em `complete()` |

Escolhida como **tabela própria**, não um terceiro tipo de item em `PushInstallations`: essa
tabela já está documentada (`docs/data-model.md`, Fase 3B.5) como tendo exatamente dois tipos de
item autoritativos (`INSTALLATION#`/`TOKEN#`); adicionar um terceiro tipo, com propósito e
padrão de acesso completamente diferentes (deduplicação de eventos, não posse de instalação),
teria violado esse invariante documentado sem necessidade. A nova tabela usa
`device_id`/`event_id` como chave exatamente como pedido, o que também casa perfeitamente com o
padrão de acesso "obter/reivindicar por device+event" sem GSI.

### Semântica adotada: at-least-once com deduplicação, concessão (lease) e abandono explícito

**Declaração explícita do limite fundamental:** entregar exatamente uma vez ao FCM não pode ser
garantido atomicamente junto com uma escrita no DynamoDB -- sempre existe uma janela entre "o FCM
aceitou a mensagem" e "este registro foi marcado como concluído de forma durável" em que um crash
(ou um abandono deliberado após um envio parcial) força uma tentativa retomada a rodar o fan-out
inteiro de novo, podendo notificar de novo uma instalação que a tentativa anterior já tinha
alcançado. Esta implementação escolhe deliberadamente **at-least-once com deduplicação**, não
"exactly-once": uma repetição rara de notificação é um custo aceitável; perder um toque
silenciosamente não é. Não há checkpoint por instalação (ver item 9) -- uma tentativa retomada
sempre refaz o fan-out inteiro.

1. `claim()` faz um `PutItem` condicional (`attribute_not_exists(device_id)`). A primeira chamada
   que vence a condição reivindica o registro com `status=PROCESSING`, `attempt=1` e
   `lease_expires_at = agora + LEASE_SECONDS` (30 segundos -- ver item 4 para a análise completa
   de temporização), e prossegue para o fan-out.
2. Uma chamada concorrente ou repetida que perde a condição inicial lê o registro existente:
   - `status=COMPLETED` → `DUPLICATE_COMPLETED`: uma tentativa anterior já terminou. Sucesso,
     **sem reenviar**. Este estado é terminal e nunca é retomado.
   - `status=PROCESSING` e a concessão **ainda não expirou** → `DUPLICATE_IN_FLIGHT`: outra
     tentativa está genuinamente em andamento agora, **ou** uma tentativa que abandonou já
     liberou a concessão e ninguém tentou retomar ainda (a concessão liberada por `abandon()` é
     imediatamente "expirada", então o próximo `claim()` já a encontra pronta para retomada, não
     neste ramo -- ver item 3).
   - `status=PROCESSING` e a concessão **já expirou** (naturalmente, ou por `abandon()`) → a
     chamada tenta roubar a concessão atomicamente via `UpdateItem` condicionado exatamente ao
     `lease_expires_at`/`attempt` que acabou de ler. No máximo um concorrente vence essa condição
     (garantia do próprio DynamoDB para escritas condicionais); o vencedor recebe `RESUMED` com
     `attempt` incrementado e prossegue com um fan-out completo; qualquer perdedor recebe
     `DUPLICATE_IN_FLIGHT`.
3. `abandon()` é como uma chamada que **reconhece a própria falha como recuperável** (falha total
   de autenticação/configuração do Firebase, ou falha temporária que nunca se resolveu -- ver
   item 6) sai de cena *imediatamente*, em vez de deixar uma concessão com aparência de "ainda
   viva" até `LEASE_SECONDS` se esgotar sozinho. Ela define `lease_expires_at = agora` (uma
   `UpdateItem` condicionada exatamente a `attempt`, mesma disciplina de `complete()`), então o
   **próximo** `claim()` -- mesmo chegando um segundo depois -- já encontra a concessão expirada
   e retoma via o mesmo caminho do item 2. `status` continua `PROCESSING`; nenhum terceiro valor
   de status foi criado. Isso é o que resolve a lacuna real identificada em revisão: um retry que
   chegue *durante* o que seria a janela de 30 segundos da concessão original não fica mais preso
   em `DUPLICATE_IN_FLIGHT` sem uma segunda chance garantida -- ele já encontra a concessão
   liberada.
4. `complete()` só marca o registro como `COMPLETED` se `attempt` ainda corresponder ao que a
   chamada recebeu de `claim()`/`RESUMED` -- uma condição (`attempt = :attempt`) impede que uma
   tentativa antiga e atrasada (que perdeu a concessão para uma tentativa retomada mais nova)
   sobrescreva o estado da tentativa vencedora. Se a condição falhar, `complete()` simplesmente
   não faz nada (não é um erro) -- dois donos nunca podem acreditar simultaneamente que
   concluíram a mesma entrega. `abandon()` tem exatamente a mesma proteção: uma tentativa
   antiga que finalmente percebe sua própria falha e chama `abandon()` com um `attempt` já
   superado nunca perturba a tentativa mais nova que já assumiu a concessão.
5. **Duração da lease calibrada contra dois números reais, não "cerca de um minuto":**
   - o timeout real do próprio Lambda `push_sender` é **20 segundos**
     (`infrastructure/stacks/notification_stack.py`) -- a AWS impõe isso como teto absoluto, então
     nenhuma execução real pode legitimamente segurar a concessão por mais tempo que isso;
   - o comportamento documentado da AWS para uma invocação **assíncrona** que falha (exatamente
     como `telemetry_ingestion` invoca `push_sender`) é repetir automaticamente, com uma espera de
     aproximadamente um minuto antes da primeira repetição e mais dois minutos antes da segunda
     (a AWS não garante contratualmente o valor exato, só que ele aumenta a cada tentativa).

   `LEASE_SECONDS = 30` fica deliberadamente entre esses dois números: 10 segundos (50%) maior
   que o teto real de execução (nunca confunde uma execução legítima com uma travada), e bem
   abaixo da metade do ~1 minuto até a *primeira* repetição automática -- de modo que mesmo essa
   primeira repetição, não só a segunda, já encontraria uma concessão abandonada/crashada
   expirada. O caminho de `abandon()` (item 3) existe justamente para que uma falha *reconhecida*
   não dependa dessa temporização; a matemática da concessão só precisa valer para um crash
   *abrupto e não reconhecido* (item 8). Um teste (`tests/unit/test_notification_stack.py`)
   sintetiza a stack real e compara `LEASE_SECONDS` contra o `Timeout` de fato do
   `AWS::Lambda::Function`, para que essa relação nunca se perca silenciosamente numa mudança
   futura em só um dos dois lados.
6. **Falha sistêmica (auth total, ou temporária não resolvida) nunca completa silenciosamente,
   mesmo com sucesso parcial.** Duas classes de falha são tratadas como "esta tentativa não pode
   ser considerada concluída":
   - **Falha total de autenticação/configuração do Firebase** -- exceção (`FirebaseCredentialError`)
     ou resultado tipado (`FcmResult(outcome="AUTH_OR_CONFIG_ERROR")`) -- interrompe todo envio
     adicional naquela invocação (as instalações restantes são contabilizadas em
     `auth_config_failure_count` sem nunca serem realmente tentadas, já que o token de acesso em
     si está com problema, não uma instalação específica).
   - **Falha temporária que persiste** mesmo depois dos retries locais de
     `fcm_client.send_with_retry()` (seção 9) -- não interrompe outras instalações (um rate limit
     numa instalação não diz nada sobre as demais), mas também marca a tentativa como não
     concluível.

   Em **ambos os casos**, mesmo que `sent_count > 0` (algumas instalações já alcançadas com
   sucesso antes da falha), o handler chama `idempotency.abandon()` e **propaga** a falha em vez
   de chamar `complete()`. Marcar `COMPLETED` aqui -- só porque *algumas* instalações tiveram
   sucesso -- descartaria silenciosamente para sempre qualquer instalação que nunca chegou a ser
   tentada de verdade. É aceitável que uma instalação já alcançada receba uma notificação
   duplicada rara quando a tentativa retomada refizer o fan-out inteiro; não é aceitável abandonar
   silenciosamente as que restaram. Nenhuma instalação/token é removido nesse caminho -- só
   `INVALID_TOKEN` (seção 9) aciona remoção.
7. **Não usa GSI eventualmente consistente como autoridade**: toda leitura desta tabela usa
   `ConsistentRead=True` na tabela base; não há GSI nela.
8. **Crash abrupto, sem `abandon()`, continua recuperável só pela concessão.** Se o processo
   morrer antes de conseguir chamar `abandon()` (ex.: um bug realmente não previsto, não uma das
   duas classes de falha reconhecidas no item 6), a recuperação cai de volta exatamente na
   temporização do item 5 -- a concessão de 30 segundos, não o TTL de 2 horas, ainda é o
   mecanismo de recuperação para esse caso. `abandon()` é um caminho *mais rápido* para falhas
   reconhecidas, não uma substituição da rede de segurança da concessão.
9. **Sem checkpoint por instalação.** Uma tentativa `RESUMED` reexecuta o fan-out inteiro a
   partir do zero -- ela não sabe quais instalações específicas a tentativa anterior já
   alcançou. Isso é uma escolha deliberada de escopo, não um descuido: implementar checkpoint
   autoritativo por instalação exigiria um item adicional por instalação por entrega, testável
   separadamente, e a diretriz desta correção prioriza explicitamente "at-least-once com
   deduplicação e lease" sobre "nunca duplicar uma única instalação" (ver a declaração do limite
   fundamental no início desta seção). Fica registrado como possível evolução futura caso o
   volume de retomadas reais em produção mostre que duplicatas são incômodas o bastante para
   justificar a complexidade adicional.

## 4. Destinatários

Para `RING_DETECTED`:

Antes do fan-out, a elegibilidade temporal usa o `occurred_at` original: início com idade maior ou
igual a 30 segundos é terminalmente suprimido; `RING_ENDED` usa 60 segundos. Timestamp ausente é
marcado internamente como desconhecido e também suprimido, sem impedir o histórico de telemetria.
Essa decisão ocorre depois do claim idempotente e antes de instalações, segredo Firebase ou FCM,
e fica auditável no próprio registro de push delivery. O app e o `expires_at` permanecem defesas
adicionais, não a primeira barreira contra alerta tardio.

1. `memberships.active_memberships()` consulta `DeviceMemberships` pela chave primária
   (`device_id`, `Query` fortemente consistente -- o padrão de acesso "obter membros de um
   dispositivo" já documentado em `docs/data-model.md` desde a Fase 1C), filtra
   `status == "ACTIVE"` e `role` em `{OWNER, ADMIN, MEMBER}`, e para de acumular (reportando
   `truncated=True`, apenas logado como aviso) em `MAX_MEMBERSHIPS_PER_DEVICE = 50`.
2. Cada membership ativo tem sua `notification_preferences` avaliada **separadamente** (ver
   seção 5).
3. Para os `user_id`s cuja avaliação não suprimiu o evento, `installations.active_installations()`
   consulta o GSI `*-push-installations-by-user-index` (projeção `KEYS_ONLY`, Fase 3B.5) por
   `user_id`, obtém os `installation_id`s, deduplica (defensivamente -- a consulta não deveria
   produzir duplicatas, mas o código nunca confia nisso) e faz `BatchGetItem` (lotes de até 100,
   com retry exponencial+jitter para `UnprocessedKeys`, até 3 tentativas) na tabela base para
   ler o item `INSTALLATION#<id>`/`INSTALLATION` completo -- é aí, e só aí, que o token mora. O
   item `TOKEN#<hash>`/`CLAIM` nunca é lido aqui; ele existe apenas para a exclusividade de posse
   na escrita (Fase 3B.5).
4. Fan-out total de instalações é limitado a `MAX_INSTALLATIONS_PER_DEVICE = 200` (mesma
   semântica de truncamento logado, não erro).
5. Ausência de membros ativos (`no_recipients`), preferências que suprimem todo mundo
   (`all_suppressed`) ou ausência de instalações para quem sobrou (`no_installations`) são
   resultados válidos e distintos, todos concluindo a idempotência normalmente -- nunca uma
   falha genérica.
6. Tokens nunca aparecem em logs ou métricas (ver seção 11).

## 5. Avaliador de preferências

`domain/push/preferences.py` é **puro**: recebe `event_type`, o dicionário já normalizado de
`notification_preferences` (produzido por
`lambdas/device_api/notification_preferences.combine()` -- a mesma função que já normaliza/valida
para as rotas GET/PATCH existentes) e `now` (UTC, aware); devolve um `Decision` tipado
(`delivery_mode`, `suppressed`, `quiet_active`, `quiet_reduced`, `reason`). Nunca toca
AWS/FCM/rede/relógio.

`delivery_mode` é sempre um dos **quatro valores de `alert_mode` que já existem no contrato**
(`NONE`, `RING_ONLY`, `NOTIFICATION_ONLY`; `RING_AND_NOTIFICATION` é legado e equivale a `RING_ONLY`, ver
`lambdas/device_api/notification_preferences.py`) -- nenhuma enumeração nova foi criada.
`"NONE"` significa suprimido (nenhuma mensagem FCM deve ser enviada); os outros três valores são
exatamente o `presentation_intent` que o payload FCM carrega.

### Preferências ausentes, legadas ou inválidas

`combine(None)` já é o fallback seguro documentado desde a Fase 3
(`docs/notification-preferences.md`): `alert_mode=RING_ONLY`, quiet desabilitado. O
`handler.py` do sender chama `combine(stored)` (capturando `ValueError`/`TypeError`) antes de
chamar `evaluate()`; qualquer falha de parsing/validação cai para `combine(None)`. Isso significa
que uma preferência corrompida ou de formato antigo se comporta exatamente como um usuário que
nunca configurou nada -- nunca derruba o fan-out dos demais membros, e nunca precisa de uma
segunda política "seguro por padrão" inventada à parte: reaproveita a que já existe.

### Quiet schedule

`_quiet_active()` converte `now` para o fuso IANA configurado via `zoneinfo` (portanto já ciente
de horário de verão) e avalia a janela como **duas sub-janelas semiabertas**, para atribuir
corretamente a madrugada ao dia em que a janela **começou**:

- parte da noite: dia da semana local de `now` está em `days` E hora local `>= start_time`;
- parte da madrugada (só quando a janela cruza meia-noite, isto é `start_time > end_time`): o
  dia da semana **anterior** ao de `now` está em `days` E hora local `< end_time`.

O intervalo é semiaberto `[start_time, end_time)`: início **inclusivo**, fim **exclusivo**. O
contrato v1 (`docs/notification-preferences.md`) não especificava essa fronteira explicitamente;
esta é a escolha desta entrega, documentada e testada nos dois limites exatos (início e fim,
janela no mesmo dia e cruzando meia-noite).

`enabled=false` ignora a agenda inteiramente, mesmo que `days`/`start_time`/`end_time` ainda
contenham um valor salvo anteriormente (o PATCH só limpa o que o cliente altera).

## 6. Matriz de comportamento

A agenda **nunca concede** uma capacidade que o `alert_mode` base já não tinha -- ela só pode
*remover*. A tabela completa (`now` fora ou dentro da janela ativa):

| `alert_mode` | Fora da janela / quiet desabilitado | Janela ativa, `behavior=NOTIFICATION_ONLY` | Janela ativa, `behavior=BLOCK_ALL` |
| --- | --- | --- | --- |
| `NONE` | suprimido (`ALERT_MODE_NONE`) | suprimido (agenda nunca habilita) | suprimido |
| `RING_ONLY` | `RING_ONLY` | suprimido (`QUIET_NOTIFICATION_ONLY_ELIMINATED_RING_ONLY`) -- perde o toque e não tinha notificação base para sobrar | suprimido (`QUIET_BLOCK_ALL`) |
| `NOTIFICATION_ONLY` | `NOTIFICATION_ONLY` | `NOTIFICATION_ONLY` (nada muda -- já não tinha toque) | suprimido |
| `RING_AND_NOTIFICATION` (legado) | `RING_ONLY` | suprimido como `RING_ONLY` | suprimido |

Cada célula não suprimida corresponde a exatamente um `presentation_intent` no payload FCM (seção
7); a suprimida nunca gera uma chamada ao FCM. Esta tabela é reproduzida integralmente pelos
testes parametrizados em `tests/unit/test_domain_push_preferences.py`.

## 7. Payload FCM (HTTP v1)

`domain/push/payload.compose_message()` monta exatamente:

```json
{
  "message": {
    "token": "<token da instalação>",
    "data": {
      "push_contract_version": "1",
      "event_id": "evt-...",
      "device_id": "ib-...",
      "event": "RING_DETECTED",
      "presentation_intent": "RING_ONLY | NOTIFICATION_ONLY",
      "occurred_at": "2026-08-20T12:00:00Z"
    },
    "android": {"priority": "high", "ttl": "30s"}
  }
}
```

Requisitos atendidos:

- **API HTTP v1**, nunca a API legada (`fcm.googleapis.com/v1/projects/{project_id}/messages:send`).
- Nenhum token, membership, e-mail ou identificador interno além do necessário para o roteamento
  do próprio FCM (`message.token`, exigido pela API para endereçar o dispositivo -- nunca
  aparece em log, ver seção 11).
- Nenhum nome de dispositivo, nem qualquer outro texto fornecido pelo firmware, é usado como
  título/corpo -- porque **não existe** um bloco `notification` neste payload (ver "Limitações
  conhecidas" abaixo).
- `presentation_intent` reutiliza exatamente os valores de `alert_mode` (seção 5/6) -- uma única
  mensagem tipada por evento, nunca duas mensagens FCM para representar a mesma decisão.
- Prioridade `high` e TTL de 30s: um toque de campainha é intrinsecamente urgente e perde o
  sentido depois de um tempo curto.

### O que foi exercitado no app (e o que fica para a 3B.9)

Este payload é **somente dados** (sem bloco `notification`), deliberadamente. A fatia mínima já
presente no app foi exercitada: recebeu os dados e apresentou corretamente a notificação Android.
Isso não equivale à experiência visual/sonora completa de "chamada recebida", que permanece na
Fase 3B.9, nem especifica todas as combinações de `presentation_intent`.

## 8. Credenciais Firebase

**Nenhuma credencial foi criada, nem será, por este código.** `lambdas/push_sender/firebase_auth.py`
apenas *consome* um secret que deve existir previamente.

### Como o sender usa a credencial

- `TokenProvider` (referência configurável por nome via
  `FIREBASE_CREDENTIALS_SECRET_NAME`, resolvida em `infrastructure/config/notifications.py`)
  busca o secret via `secretsmanager:GetSecretValue`, parseia o JSON de service account e
  constrói `google.oauth2.service_account.Credentials` (biblioteca `google-auth`, mantida pelo
  Google -- ver "Por que google-auth, não uma implementação própria" abaixo).
- A credencial parseada e o access token atual ficam em cache **somente em memória**, pela vida
  da instância Lambda (isto é, não persistem entre invocações frias); nunca são gravados em log,
  output do CloudFormation ou fixture.
- Renovação: o token é reutilizado enquanto faltar mais de `REFRESH_SKEW_SECONDS = 300` segundos
  para expirar; dentro dessa margem, é renovado antes de ser usado.
- Timeout explícito de `TOKEN_REQUEST_TIMEOUT_SECONDS = 5` segundos na troca do JWT assertion por
  access token (o padrão do `google-auth` é 120s; um wrapper (`_bounded_timeout_request`) reduz
  isso explicitamente, já que 120s é tempo demais para o caminho crítico de uma Lambda).
- Timeout explícito de `FCM_REQUEST_TIMEOUT_SECONDS = 8` segundos no próprio envio FCM
  (`fcm_client.py`).

### Por que google-auth, não uma implementação própria

Trocar um service account por um access token OAuth2 exige assinar um JWT com RSA-SHA256 -- algo
que a stdlib do Python não faz sem uma biblioteca de criptografia. A decisão registrada nesta
entrega foi usar `google-auth` (mantida pelo Google, auditada, amplamente usada) em vez de
implementar RSA/PKCS#8/JWT manualmente: mesmo esta base de código preferindo historicamente evitar
dependências (todos os outros Lambdas usam só `boto3`), autenticação é exatamente o tipo de
componente onde reduzir a superfície de "código criptográfico escrito à mão" pesa mais do que
manter zero dependências.

### Empacotamento (sem binários no git, sem `pip install` em runtime)

`lambdas/push_sender/requirements.txt` fixa exatamente `google-auth==2.35.0` e `requests==2.32.3`
(a segunda é o transporte HTTP que o próprio `google-auth` usa para a troca de token, e também o
cliente HTTP do envio FCM), mais toda dependência transitiva pinada explicitamente
(`cachetools`, `certifi`, `charset-normalizer`, `idna`, `pyasn1`, `pyasn1-modules`, `rsa`,
`urllib3`).

`infrastructure/stacks/notification_stack.py` empacota essas dependências via **bundling Docker
do construct `aws_lambda.Function` estável** (`aws_cdk.aws_lambda.Code.from_asset(...,
bundling=BundlingOptions(...))`) -- não o módulo experimental `aws_lambda_python_alpha` (que este
projeto nunca usou e que ficaria pinado a uma versão específica de `aws-cdk-lib` de forma
frágil), e não uma Layer com wheels versionadas no git. A imagem de build é a mesma que o
runtime Lambda Python 3.12 usa (`Runtime.PYTHON_3_12.bundling_image`), e o comando de bundling
roda:

```bash
pip install --no-cache-dir \
  --platform manylinux2014_aarch64 --python-version 3.12 --implementation cp --abi cp312 \
  --only-binary=:all: \
  -r lambdas/push_sender/requirements.txt -t /asset-output
```

Os flags `--platform`/`--python-version`/`--implementation`/`--abi`/`--only-binary=:all:` forçam
o pip a baixar os wheels pré-compilados exatos para Linux/ARM64/CPython 3.12 (a arquitetura real
do Lambda, `Architecture.ARM_64`), **independentemente da arquitetura da máquina que roda
`cdk synth`/`cdk deploy`** -- verificado localmente: o pacote `charset-normalizer` (que tem
extensão compilada) resulta em `.so` de fato `aarch64-linux-gnu`, mesmo sintetizando em uma
máquina Windows/x86. Isso é o que torna o artefato **reproduzível**: o mesmo `requirements.txt`
produz o mesmo conjunto de wheels em qualquer host de build, sem depender de qual arquitetura
esse host tem.

`domain/` e `lambdas/` são copiados para dentro do mesmo asset (`cp -r`), do mesmo jeito que
`lambdas/telemetry_ingestion` já faz (asset de repositório inteiro, não o asset restrito
`lambdas/` que as Lambdas de API Gateway usam) -- por isso o sender pode importar
`domain.telemetry.models` (reuso do parser canônico) e
`lambdas.device_api.notification_preferences` (reuso do `combine()`) sem duplicar nenhuma dessas
duas peças de lógica.

**Nenhum wheel, binário ou credencial é versionado neste repositório.** O bundling roda a cada
`cdk synth`/`cdk deploy`, com rede (para baixar os pacotes pinados do PyPI) e Docker disponíveis
no host -- exatamente o que `ubuntu-latest` do GitHub Actions já oferece por padrão, sem setup
adicional. Isso é uma mudança real de comportamento da CI: o passo `cdk synth` agora baixa uma
imagem Docker (~300 MB, cacheada depois da primeira vez) e faz `pip install` de ~9,6 MB de
pacotes a cada execução que não tenha cache de asset -- mais lento que antes (medido localmente:
a suíte de synth passou de segundos para ~2 minutos por execução completa de `app.py`), mas é
exatamente o "CI e synth executam o mesmo bundling usado no deploy" pedido nesta entrega.

### Impacto no pacote e no cold start

O asset do `push_sender` cresce em ~9,6 MB (medido: `google-auth` + `requests` + transitivas,
sem os scripts de console/`bin/`, removidos explicitamente no bundling) sobre o código-fonte em
si. Na entrega de implementação, cold start não havia sido medido em ambiente real; o Lambda está
configurado com `memory_size=256` MB e `timeout=20` segundos, o mesmo padrão dos demais Lambdas
deste projeto.

### Procedimento manual usado para a credencial (fora do CDK)

1. No console do Firebase/Google Cloud do projeto DEV já existente (criado na Fase 3B.1-3B.4;
   este PR não cria nem altera o projeto Firebase), criar uma **service account dedicada** com o
   privilégio mínimo necessário para enviar FCM (papel `Firebase Cloud Messaging API Admin` ou
   equivalente mínimo -- não usar uma conta com privilégios amplos de projeto).
2. Gerar uma chave JSON para essa service account (Google Cloud Console → IAM → Service Accounts
   → Keys → Add key → JSON).
3. Armazenar o conteúdo desse JSON, sem modificação, como `SecretString` de um secret no AWS
   Secrets Manager da conta/região DEV, com o nome exato que
   `infrastructure/config/notifications.py` espera:
   `interbridge-dev-notifications-firebase-credentials` (o `CfnOutput`
   `FirebaseCredentialsSecretNameExpected` do `InterBridge-Dev-NotificationStack` confirma esse
   nome exato após um `cdk synth`).
4. Obter o ARN do secret criado (`aws secretsmanager describe-secret --secret-id
   interbridge-dev-notifications-firebase-credentials`) e registrá-lo em
   `CONTEXT.md`/runbook de deploy para referência futura -- este PR não precisa desse ARN exato
   para sintetizar (`Secret.from_secret_name_v2` resolve por nome, sem lookup real na AWS em
   tempo de synth), mas o deploy real depende do secret já existir.
5. **Rotação:** gerar uma nova chave JSON pela mesma service account, atualizar o `SecretString`
   do mesmo secret (`aws secretsmanager put-secret-value`) e só então revogar a chave antiga no
   Google Cloud Console -- nessa ordem, para não haver janela sem credencial válida. O sender
   busca a credencial a cada instância fria do Lambda (o cache é só de memória, por instância),
   então uma rotação se propaga naturalmente conforme instâncias antigas são recicladas, sem
   precisar de um redeploy.
6. **Revogação de emergência:** desabilitar/excluir a chave da service account no Google Cloud
   Console imediatamente; instâncias Lambda já quentes com token em cache continuam funcionando
   até o token OAuth2 expirar (até 1 hora) ou até o próximo cold start tentar renovar e falhar
   (classificado como `AUTH_OR_CONFIG_ERROR`, sem apagar nenhuma instalação -- ver seção 9).

## 9. Resultados e tokens inválidos

`domain/push/fcm_result.classify(http_status, body)` é **puro** e devolve um destes:

| `outcome` | Gatilho | Ação do sender |
| --- | --- | --- |
| `SUCCESS` | `2xx` | `sent_count += 1` |
| `INVALID_TOKEN` | `404`, ou `errorCode == "UNREGISTERED"` no corpo estruturado do FCM | remove a instalação (ver abaixo) |
| `AUTH_OR_CONFIG_ERROR` | `401`/`403` | `auth_config_failure_count += 1`, token preservado |
| `RATE_LIMITED` | `429`, ou `errorCode == "QUOTA_EXCEEDED"` | `temporary_failure_count += 1`, token preservado |
| `TEMPORARY_ERROR` | `5xx`, `errorCode` em `{UNAVAILABLE, INTERNAL, UNSPECIFIED_ERROR}`, falha de rede/timeout (sem status HTTP algum), ou qualquer status não reconhecido | `temporary_failure_count += 1`, token preservado |
| `PERMANENT_PAYLOAD_ERROR` | `400` sem ser sobre o token | `permanent_failure_count += 1`, token preservado |

**Apenas `UNREGISTERED`** aciona remoção -- deliberadamente conservador: `SENDER_ID_MISMATCH`
(que também é um `403`) poderia indicar erro de configuração do lado do backend, não
necessariamente um token morto, então não apaga nada. `429`/`5xx`/erro genérico nunca apagam
token, exatamente como pedido -- inclusive depois de esgotados os retries locais descritos abaixo.

### Falha temporária que persiste: nunca vira sucesso silencioso

Uma falha `RATE_LIMITED`/`TEMPORARY_ERROR` que **não se resolve** mesmo depois dos retries locais
de `fcm_client.send_with_retry()` (ver "Retries" abaixo) não é apenas contabilizada e esquecida --
ela marca a tentativa inteira como não conclusível, pelo mesmo mecanismo de `abandon()` +
propagação usado para falha total de autenticação (seção 3, item 6): a diferença é que uma falha
temporária **não** interrompe as demais instalações (um rate limit numa instalação não indica
nada sobre as outras), mas ainda assim impede `complete()` de rodar. Isso significa que `429`s ou
`5xx`s persistentes resultam em uma nova tentativa completa do evento (com o mesmo custo de
duplicata rara documentado na seção 3), nunca em uma entrega marcada como concluída enquanto uma
instalação nunca foi de fato alcançada.

### Remoção transacional

`cleanup.delete_invalid_installation()` reusa a mesma transação de duas escritas que Fase 3B.5 já
definiu para exclusividade: `TransactWriteItems` apaga `INSTALLATION#<id>`/`INSTALLATION` **e**
`TOKEN#<hash>`/`CLAIM` juntos, condicionados a `user_id`/`token_hash` (instalação) e a
`claimed_installation_id`/`claimed_user_id` (claim) serem exatamente os que o sender acabou de
ler. Uma corrida com um `PUT /v1/push/installations/{id}` concorrente (login, logout, rotação de
token) muda esses valores e faz a condição falhar -- a exclusão é então silenciosamente pulada
(`False`, não uma exceção), porque a instalação que existia quando o FCM respondeu já não é a
mesma que existe agora. O resultado do envio (`invalid_token_count`) já foi registrado antes
dessa tentativa de limpeza, então uma falha ao limpar nunca esconde o resultado original do
envio.

### Retries

Dois níveis de retry, deliberadamente com escopos diferentes:

1. **`BatchGetItem` de instalações** (`installations.py`): backoff exponencial + jitter, apenas
   para `UnprocessedKeys` -- uma condição que a própria API do DynamoDB já sinaliza como "tente de
   novo", nunca para uma resposta de erro definitiva.
2. **Envio FCM por instalação** (`fcm_client.send_with_retry()`): quando `classify()` devolve
   `RATE_LIMITED` ou `TEMPORARY_ERROR` -- nunca para `INVALID_TOKEN`, `AUTH_OR_CONFIG_ERROR` ou
   `PERMANENT_PAYLOAD_ERROR`, que uma nova tentativa imediata não resolve -- até
   `MAX_SEND_ATTEMPTS = 3` tentativas totais (1 inicial + 2 retries) **por instalação**, com
   backoff (`0.2s`, depois `0.4s`, mais jitter pequeno) ou respeitando um cabeçalho `Retry-After`
   numérico quando o FCM envia um (a forma HTTP-date de `Retry-After` não é interpretada; nesse
   caso o backoff padrão é usado). Todo delay é limitado a `MAX_RETRY_DELAY_SECONDS = 2` segundos,
   mesmo que o `Retry-After` peça mais -- isso, somado ao limite de 3 tentativas por instalação,
   é o que impede que uma tempestade de `429`s vire uma tempestade de requisições ou consuma
   sozinha o timeout de 20 segundos da função num fan-out grande.

   Se, mesmo depois desses retries locais, o resultado continuar `RATE_LIMITED`/`TEMPORARY_ERROR`,
   ver "Falha temporária que persiste" acima -- não é mais um retry local, é a idempotência da
   seção 3 que assume a partir daí (abandono + nova tentativa completa do evento).

## 10. Infraestrutura AWS (somente o necessário para DEV)

`infrastructure/stacks/notification_stack.py` (novo) depende só de `DataStack`:

- Lambda `push_sender` (`interbridge-dev-notifications-push-sender`), `PYTHON_3_12`/`ARM_64`,
  memória 256 MB, timeout 20s, sem `ReservedConcurrentExecutions`.
- Configuração de invocação assíncrona: `retry_attempts=2`, destino de falha
  (`on_failure`) uma fila SQS dedicada (`interbridge-dev-notifications-push-sender-dlq`,
  retenção de 4 dias, criptografia gerenciada pela SQS) -- nunca reaproveita a `technical-dlq` da
  Fase 1E, mantendo os domínios de falha separados.
- Referência (não criação) ao secret Firebase via `Secret.from_secret_name_v2`.
- IAM de privilégio mínimo (detalhado na seção 9/tabela de IAM abaixo) -- nenhum `dynamodb:*`,
  `secretsmanager:*` ou `Resource: "*"`.

`infrastructure/stacks/data_stack.py` ganhou a sétima tabela,
`interbridge-dev-push-notification-deliveries` (seção 3), com as mesmas propriedades padrão de
todas as demais tabelas deste projeto: `PAY_PER_REQUEST`, chave gerenciada pela AWS, PITR
desligado em DEV, `deletion_protection=True` + `RemovalPolicy.RETAIN`, sem stream, sem Global
Table.

`infrastructure/stacks/ingestion_stack.py` ganhou um parâmetro opcional `push_sender_function`:
quando fornecido (como `app.py` agora faz), adiciona a variável de ambiente
`PUSH_SENDER_FUNCTION_NAME` e uma única permissão IAM (`lambda:InvokeFunction`, escopada ao ARN
exato da função) ao papel do `telemetry_ingestion`. Quando omitido (como os testes existentes
desta stack continuam fazendo), o comportamento é idêntico ao de antes desta entrega -- nenhum
recurso extra, nenhuma env var extra.

### Tabela de permissões IAM (push_sender)

| Ação | Recurso | Motivo |
| --- | --- | --- |
| `dynamodb:PutItem`, `GetItem`, `UpdateItem` | tabela `push-notification-deliveries` | reivindicar/concluir idempotência |
| `dynamodb:Query` | tabela `device-memberships` (base) | listar membros ativos do device |
| `dynamodb:Query` | índice `push-installations-by-user-index` | descobrir `installation_id`s por usuário |
| `dynamodb:BatchGetItem`, `DeleteItem` | tabela `push-installations` (base) | ler instalações; remoção transacional de token inválido |
| `secretsmanager:GetSecretValue` | secret Firebase exato | trocar credencial por access token |
| `logs:CreateLogStream`, `PutLogEvents` | log group da função | logs padrão |

`telemetry_ingestion` ganhou, apenas quando `push_sender_function` é fornecido:

| Ação | Recurso | Motivo |
| --- | --- | --- |
| `lambda:InvokeFunction` | ARN exato do `push_sender` | disparo assíncrono após persistir `RING_DETECTED` |

### `cdk diff`

Nenhuma credencial AWS real esteve disponível neste ambiente de desenvolvimento para rodar um
`cdk diff` autêntico contra a conta DEV -- consistente com o próprio princípio deste projeto de
que `cdk synth` nunca depende de credenciais, e que `cdk diff`/`cdk deploy` são sempre passos
manuais revisados por um humano com acesso real (ver `docs/deployment.md`). `cdk synth` **foi**
executado localmente com sucesso, incluindo o bundling Docker real (ver seção 8), e sintetizou as
seis stacks (`Data`, `IoT`, `Api`, `Notification`, `Ingestion`, `Observability`) sem erros. Os
recursos novos/alterados, deduzidos dos templates sintetizados (não de um `cdk diff` real):

- **Novo** `InterBridge-Dev-DataStack`: uma tabela DynamoDB adicional
  (`push-notification-deliveries`), sem alteração nas seis tabelas existentes.
- **Nova stack** `InterBridge-Dev-NotificationStack`: 1 função Lambda, 1 fila SQS, 1 log group, 1
  papel/política IAM, 1 `EventInvokeConfig`, 1 referência a secret (não cria o
  `AWS::SecretsManager::Secret` em si).
- **Alterado** `InterBridge-Dev-IngestionStack`: a função `telemetry_ingestion` ganha uma
  variável de ambiente (`PUSH_SENDER_FUNCTION_NAME`) e uma statement IAM adicional
  (`lambda:InvokeFunction`); nenhum recurso é substituído (`Replacement`), apenas atualizado.
- **Alterado** `InterBridge-Dev-ObservabilityStack`: três alarmes CloudWatch adicionais (erros,
  throttles e mensagens visíveis na DLQ assíncrona do `push_sender`).

Antes de qualquer deploy real, um `cdk diff` genuíno contra a conta DEV deve ser revisado por um
humano com credenciais, como já é prática estabelecida neste projeto.

## 11. Observabilidade

Logs estruturados (JSON, via `logging`) em cada etapa relevante: evento recebido/aceito/rejeitado,
decisão agregada do fan-out (`push_sender_completed`, com todos os contadores), remoção de token
(`push_sender_token_removed`/`_skipped_race`), falha ao disparar a invocação
(`push_trigger_failure`, em `telemetry_ingestion`). **Nunca** incluem token FCM, credencial
Firebase, access token OAuth ou o corpo bruto de uma resposta HTTP não validada -- os testes
(`tests/unit/test_push_sender_handler.py::test_logs_never_contain_the_push_token` e as suítes de
`firebase_auth`/`fcm_client`) verificam isso diretamente.

Métricas CloudWatch (namespace `InterBridge/PushSender`, formato EMF -- sem dependência nova,
sem chamada de API adicional, sem dimensão de alta cardinalidade como `device_id`/`user_id`/
`event_id`): `EventsReceived`, `EventsRejected`, `EventsDuplicate`, `EventsProcessed`,
`MembershipsFound`, `InstallationsFound`, `Sent`, `Suppressed`, `InvalidTokens`,
`TemporaryFailures`, `PermanentFailures`, `AuthConfigFailures`.

Alarmes CloudWatch (`ObservabilityStack`, quando `notification_stack` é fornecido -- como
`app.py` agora faz): erros do Lambda, throttles do Lambda, mensagens visíveis na fila de falha
assíncrona -- mesmo padrão de "quatro alarmes de baixo custo, sem dashboard" já estabelecido na
Fase 1E.

## 12. Custos esperados (DEV, estimativa)

Com o fluxo implantado em DEV, os recursos passaram a ter o perfil de custo descrito abaixo;
a validação pontual não caracteriza volume de produção. Cada toque de
campainha gera, por instalação destinatária, uma invocação Lambda (~1-2s estimados, não medidos),
uma chamada HTTPS ao FCM, no máximo uma renovação de token OAuth2 a cada ~55 minutos por instância
Lambda quente, e algumas leituras/escritas DynamoDB on-demand -- tudo dentro da mesma ordem de
grandeza de baixo custo que os outros Lambdas deste projeto (ver `docs/cost-controls.md`). O custo
novo mais visível é o próprio secret no Secrets Manager (~US$0,40/mês por secret, cobrança padrão
da AWS, independente de uso).

## 13. Limitações conhecidas

- **A validação visual foi somente da fatia mínima existente no app.** A notificação apareceu no
  Android, mas a apresentação visual/sonora completa de chamada, todos os modos de preferência e
  o ciclo de vida completo da 3B.9 não foram validados.
- **Uma falha reconhecida (auth total, ou temporária persistente) é abandonada e propagada
  imediatamente** (seção 3, itens 3 e 6) -- o próximo retry, mesmo chegando um segundo depois,
  já retoma sem esperar a concessão expirar sozinha. Só um crash **abrupto e não reconhecido**
  (seção 3, item 8) ainda depende da concessão de 30 segundos se esgotar naturalmente. Em ambos
  os casos, uma tentativa retomada reexecuta o fan-out inteiro (sem checkpoint por instalação,
  seção 3, item 9), então, raramente, uma instalação já alcançada por uma tentativa anterior pode
  receber uma segunda notificação para o mesmo toque -- troca deliberada (at-least-once),
  documentada na seção 3, não um bug.
- **DST/mudanças de fuso horário do lado do usuário** (ex.: usuário muda o fuso do celular) não
  são tratadas aqui -- a `notification_preferences.quiet_schedule.timezone` é o fuso salvo pelo
  usuário, não detectado automaticamente por presença/localização (documentado como fora de
  escopo desde a Fase 3 original, `docs/notification-preferences.md`).
- **A fonte física validada não foi o Linker Button.** O firmware 3B.8 (PR #20 de
  `hjca14/interBridge`) foi executado em ESP32-C3 Super Mini; no teste, GPIO4 ficou em LOW por um
  resistor de aproximadamente 10 kΩ para GND e foi levado momentaneamente a 3V3. GPIO4 continua
  uma sobreposição DEV provisória com o DRX do Si3050, não uma definição de produção. Linker
  Button, Si3050 real, linha de interfone real, áudio e comportamento offline/replay não foram
  validados.
- **iOS/APNs não fazem parte desta entrega** -- reservado para Fase 3B.10.

## Roadmap (3B.5-3B.10)

- **3B.5:** backend, contrato e ciclo de instalações validados ponta a ponta em DEV pelo app. O
  login criou instalação e claim; o reinício preservou `installation_id` e `created_at`, atualizou
  `updated_at` e não duplicou registros; o logout removeu instalação e claim antes do sign-out; e
  um novo login recriou ambos com o mesmo `installation_id`.
- **3B.6:** sender FCM implantado e validado em DEV.
- **3B.7:** aplicação das preferências e quiet mode implantada; o caminho usado pelo evento foi
  validado em DEV, sem alegar cobertura operacional de todas as combinações.
- **3B.8:** simulador físico mergeado no firmware e exercitado em ESP32 real nas condições DEV
  delimitadas acima.
- **3B.9:** a fatia mínima do app apresentou a notificação; a experiência completa de chamada
  Android permanece aberta.
- **3B.10:** iOS/APNs.

## Validação ponta a ponta em DEV

Após o deploy e a validação inicial com evento sintético, o firmware do PR #20 de
`hjca14/interBridge` conectou a Wi-Fi, sincronizou NTP e conectou à AWS IoT por MQTT/mTLS em um
ESP32-C3 Super Mini. O health report percorreu a ingestão e fez o dispositivo aparecer online no
app. Um único estímulo físico controlado nas condições descritas acima produziu exatamente um
`RING_DETECTED`. O evento percorreu firmware → AWS IoT → `telemetry_ingestion` → `push_sender` →
Firebase Cloud Messaging → app; o sender confirmou a entrega e a notificação apareceu no Android.

Portanto, 3B.6/3B.7 estão validadas ponta a ponta **em DEV**, com evento originado em hardware real,
e não apenas sintético. Isso não é validação de produção nem valida Linker Button, hardware de
interfone, Si3050, linha real, áudio, offline/replay ou a experiência completa de chamada 3B.9.
