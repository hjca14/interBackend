# Preferências de notificações por usuário e dispositivo

Esta entrega persiste `notification_preferences` na própria `DeviceMembership` identificada por
`device_id + user_id` autenticado. Assim, pessoas com acesso ao mesmo dispositivo mantêm escolhas
independentes. A membership `ACTIVE` continua sendo a autorização; memberships ausentes, pendentes,
revogadas ou inativas não dão acesso. O GET não escreve e memberships antigas permanecem compatíveis.

## Contrato v1 e defaults

O contrato completo contém `version: 1`, `incoming_calls_enabled: true`,
`notifications_enabled: true`, `delivery_scope: ANYWHERE`, `quiet_schedule` desativada (timezone e
horários nulos, dias vazios, behavior `NOTIFICATION_ONLY`) e `updated_at: null` até o primeiro PATCH. O timestamp
é UTC e gerado somente pelo servidor. PATCH é parcial, rejeita corpo vazio, campos desconhecidos,
`version` e `updated_at` e combina o estado antes de validar. Para não perder PATCHes simultâneos, a
escrita compara o mapa ao valor da leitura consistente (ou exige que continue ausente), relê e
reaplica o patch em caso de conflito, com no máximo três tentativas. Conflito persistente retorna
`409 CONFLICT`; perda de acesso durante o retry permanece `404 RESOURCE_NOT_FOUND`.

Receber **ligação** e receber **notificação comum** são escolhas independentes. No futuro, durante um
**Horário sem ligação** ou **Modo de descanso**, `NOTIFICATION_ONLY` (**Só notificação**) não permitirá
uma ligação e poderá permitir a notificação comum de que o interfone tocou somente quando
`notifications_enabled` estiver ativo. `BLOCK_ALL` (**Bloquear tudo**) não permitirá nem a ligação nem
essa notificação. A programação apenas restringe preferências globais: nunca habilita ligação ou
notificação desabilitada. Android/iOS e o usuário continuam responsáveis por volume, vibração, Não
Perturbe, permissões e canais; o InterBridge não altera controles gerais do celular.

A programação ativa exige timezone IANA (por exemplo `America/Sao_Paulo`), dias ISO 1–7 sem
repetição, e início/fim locais estritos `HH:mm` e diferentes. Intervalos podem atravessar meia-noite.
`ANYWHERE`, `LOCAL_ONLY` e `AWAY_ONLY` apenas persistem a intenção: a descoberta de presença na rede
local ainda não foi definida.

## Estado e limites

A persistência e as APIs GET/PATCH estão implementadas localmente. O contrato runtime-safe reside em
`lambdas/device_api/notification_preferences.py`, dentro do asset implantável, sem dependência
invertida de `domain/`. O deploy DEV permanece pendente.
Os filtros ainda não são aplicados a envio algum. Firebase/FCM e registro de instalações não estão
configurados; chamada recebida Android não foi implementada; PushKit/CallKit no iOS ficam para etapa
posterior. Áudio nunca trafegará por push.

Ordem preservada do roadmap:

1. preferências persistidas;
2. integração do app à API real;
3. FCM e registro de instalações;
4. aplicação dos filtros;
5. chamada recebida no Android;
6. canal de áudio;
7. chamada recebida no iOS com PushKit/CallKit;
8. onboarding BLE.
