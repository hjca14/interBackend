# Preferências de notificações por usuário e dispositivo

Esta entrega persiste `notification_preferences` na própria `DeviceMembership` identificada por
`device_id + user_id` autenticado. Assim, pessoas com acesso ao mesmo dispositivo mantêm escolhas
independentes. A membership `ACTIVE` continua sendo a autorização; memberships ausentes, pendentes,
revogadas ou inativas não dão acesso. O GET não escreve e memberships antigas permanecem compatíveis.

## Contrato v1 e defaults

O contrato completo contém `version: 1`, `incoming_calls_enabled: true`,
`notifications_enabled: true`, `delivery_scope: ANYWHERE`, `quiet_schedule` desativada (timezone e
horários nulos, dias vazios, behavior `SILENT`) e `updated_at: null` até o primeiro PATCH. O timestamp
é UTC e gerado somente pelo servidor. PATCH é parcial, rejeita corpo vazio, campos desconhecidos,
`version` e `updated_at`, combina o estado antes de validar e substitui atomicamente somente o mapa.

Receber **ligação** e receber **notificação comum** são escolhas independentes. `SILENT` representa a
intenção futura de entrega sem som/vibração quando possível; `BLOCK`, a intenção futura de não
entregar. Nenhuma dessas opções controla entrega real nesta etapa.

A programação ativa exige timezone IANA (por exemplo `America/Sao_Paulo`), dias ISO 1–7 sem
repetição, e início/fim locais estritos `HH:mm` e diferentes. Intervalos podem atravessar meia-noite.
`ANYWHERE`, `LOCAL_ONLY` e `AWAY_ONLY` apenas persistem a intenção: a descoberta de presença na rede
local ainda não foi definida.

## Estado e limites

A persistência e as APIs GET/PATCH estão implementadas localmente; o deploy DEV permanece pendente.
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
