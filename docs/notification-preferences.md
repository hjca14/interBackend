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

`NOTIFICATION_ONLY` restringe `RING_AND_NOTIFICATION` e `NOTIFICATION_ONLY` a somente notificação;
restringe `RING_ONLY` e `NONE` a nenhuma entrega. `BLOCK_ALL` restringe qualquer `alert_mode` a
nenhuma entrega durante o período. A programação nunca habilita algo desabilitado no modo-base.
Esse avaliador (`domain/push/preferences.py`) foi implementado na Fase 3B.6/3B.7 -- ver
`docs/fcm-notification-sender.md` para a matriz completa e os testes que a cobrem. Ele já é
usado pelo sender FCM implantado em DEV e foi exercitado no fluxo ponta a ponta real descrito em
`docs/fcm-notification-sender.md`.

O InterBridge não controla volume, vibração, modo silencioso, Não Perturbe, permissões gerais do
celular ou canais do sistema operacional.

## Rede e limites

Comportamentos diferentes dentro e fora da rede foram adiados. Uma possível versão futura do
contrato depende de investigação e validação real de presença em Android e iOS. Nenhuma solução foi
escolhida e não há compromisso com SSID, IP público, mDNS, geolocalização ou heartbeat. O contrato v1
não reserva campos especulativos para isso.

A persistência e as APIs GET/PATCH estão implementadas localmente. O contrato runtime-safe reside em
`lambdas/device_api/notification_preferences.py`, dentro do asset implantável, sem dependência
invertida de `domain/`. O backend e o app foram integrados em DEV. O
avaliador de filtros (`domain/push/preferences.py`) e o sender FCM (Fase 3B.6/3B.7) foram
implementados, implantados e validados com um `RING_DETECTED` originado em ESP32 real -- ver
`docs/fcm-notification-sender.md`. A notificação apareceu no Android, mas a experiência completa
de chamada continua pendente
(Fase 3B.9); chamada iOS fica para etapa posterior (Fase 3B.10). Áudio nunca trafega por push.

Ordem do roadmap:

1. persistência das preferências (concluída);
2. integração mínima do app para o fluxo validado (concluída em DEV);
3. FCM e registro de instalações (Fase 3B.5, exercitados no fluxo DEV; o ciclo completo de
   registro/remoção continua fora desta validação);
4. sender e aplicação dos filtros (Fase 3B.6/3B.7, implantados e validados E2E em DEV);
5. simulador físico no firmware (Fase 3B.8, mergeado e exercitado em ESP32 real);
6. experiência completa de chamada recebida Android (Fase 3B.9, pendente);
7. áudio;
8. chamada recebida iOS (Fase 3B.10);
9. onboarding BLE.
