# Preferências de alertas por usuário e dispositivo

Esta entrega persiste `notification_preferences` na própria `DeviceMembership` identificada por
`device_id + user_id` autenticado. Pessoas com acesso ao mesmo dispositivo mantêm escolhas
independentes. A membership `ACTIVE` continua sendo a autorização; memberships ausentes, pendentes,
revogadas ou inativas não dão acesso. O GET não escreve e memberships antigas permanecem compatíveis.

## Contrato v1 e defaults

A representação completa contém `version: 1`, `alert_mode: RING_AND_NOTIFICATION`,
`quiet_schedule` desativado (timezone e horários nulos, dias vazios, behavior
`NOTIFICATION_ONLY`) e `updated_at: null` até o primeiro PATCH. O timestamp é UTC e gerado somente
pelo servidor.

Há um único modo-base por usuário e dispositivo:

- `NONE`: não permite ligação nem a notificação comum relacionada ao toque;
- `RING_ONLY`: permite ligação, sem a notificação comum relacionada;
- `NOTIFICATION_ONLY`: não permite ligação e permite apenas a notificação comum relacionada;
- `RING_AND_NOTIFICATION`: permite ligação e também a notificação comum conforme o futuro fluxo.

Nesta entrega esses valores apenas persistem intenção. Ainda não se define quando uma notificação
comum aparecerá em relação à ligação, nem se implementa envio, chamada ou avaliação de eventos.

PATCH é parcial no topo e dentro de `quiet_schedule`, rejeita corpo vazio, campos desconhecidos,
`version` e `updated_at`, e valida o estado final combinado. Para não perder PATCHes simultâneos, a
escrita compara o mapa ao valor da leitura consistente (ou exige que continue ausente), relê e
reaplica o patch em caso de conflito, com no máximo três tentativas. Conflito persistente retorna
`409 CONFLICT`; perda de acesso durante o retry permanece `404 RESOURCE_NOT_FOUND`.

## Horários sem ligação

A programação ativa exige timezone IANA (por exemplo `America/Sao_Paulo`), dias ISO 1–7 sem
repetição e início/fim locais estritos `HH:mm` e diferentes. Intervalos podem atravessar meia-noite.

No futuro, `NOTIFICATION_ONLY` restringirá `RING_AND_NOTIFICATION` e `NOTIFICATION_ONLY` a somente
notificação; restringirá `RING_ONLY` e `NONE` a nenhuma entrega. `BLOCK_ALL` restringirá qualquer
`alert_mode` a nenhuma entrega durante o período. A programação nunca habilita algo desabilitado no
modo-base. Esse avaliador ainda não existe.

O InterBridge não controla volume, vibração, modo silencioso, Não Perturbe, permissões gerais do
celular ou canais do sistema operacional.

## Rede e limites

Comportamentos diferentes dentro e fora da rede foram adiados. Uma possível versão futura do
contrato depende de investigação e validação real de presença em Android e iOS. Nenhuma solução foi
escolhida e não há compromisso com SSID, IP público, mDNS, geolocalização ou heartbeat. O contrato v1
não reserva campos especulativos para isso.

A persistência e as APIs GET/PATCH estão implementadas localmente. O contrato runtime-safe reside em
`lambdas/device_api/notification_preferences.py`, dentro do asset implantável, sem dependência
invertida de `domain/`. O deploy DEV permanece pendente e o app ainda não está integrado. Os filtros
ainda não são aplicados. Firebase/FCM e registro de instalações não estão configurados; chamada
Android com o app fechado continua pendente; chamada iOS fica para etapa posterior. Áudio nunca
trafega por push.

Ordem preservada do roadmap:

1. persistência das preferências;
2. integração do app;
3. FCM e registro de instalações;
4. aplicação dos filtros;
5. chamada recebida Android;
6. áudio;
7. chamada recebida iOS;
8. onboarding BLE.
