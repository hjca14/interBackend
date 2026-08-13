# ADR 0001: Onboarding BLE-first, com QR e digitação manual como fallback

- **Status:** Accepted
- **Data:** 2026-08-13
- **Fase:** 1B.2 (decisão arquitetural e documental — nenhum código de BLE,
  claim ou provisioning real foi implementado por esta ADR)

## Contexto

Até a Fase 1B.1, o `CONTEXT.md` e o resumo do protocolo neste repositório
descreviam um fluxo de reivindicação de dispositivo (*claim*) centrado em
QR code como mecanismo primário:

```text
interbridge://claim?v=1&device_id=ib-<32hex>&claim_code=<segredo>
```

Esse formato ainda é o que está documentado hoje, na versão vigente (Draft
v1.2) de `interBridge/docs/communication-protocol.md`, a fonte oficial do
protocolo. O firmware (`interBridge`) e o aplicativo (`interapp`) estão
sendo atualizados **separadamente** desta tarefa para um modelo BLE-first
— este backend não pode nem deve antecipar essa mudança nos outros
repositórios, mas precisa preparar sua própria arquitetura, terminologia e
IoT Policy para o destino architected, evitando retrabalho estrutural
quando os outros repositórios convergirem.

Dois problemas concretos motivam esta ADR:

1. **Ambiguidade de nomenclatura.** O termo `claim_code` vinha sendo usado
   de forma imprecisa para dois conceitos bem diferentes: (a) um código
   humano de fabricação e (b) uma autorização de sessão do backend. Isso
   dificulta raciocinar sobre segurança — por exemplo, "quanto tempo esse
   código vive?" tinha respostas diferentes dependendo de qual dos dois
   conceitos se estava discutindo.
2. **QR como único caminho primário é frágil operacionalmente.** Exigir
   que o usuário sempre escaneie um QR físico no dispositivo (iluminação
   ruim, etiqueta danificada, QR fora de alcance da câmera) é um ponto de
   falha evitável quando o dispositivo já está fisicamente por perto e
   pode ser descoberto e confirmado via BLE.

## Decisão

Adotar **BLE-first** como fluxo primário de onboarding, com QR code e
digitação manual do mesmo código como dois fallbacks equivalentes em
segurança (nenhum dos dois é "mais seguro" que o outro — ambos carregam
apenas um identificador de conveniência, nunca uma credencial de posse
completa por si só).

```text
PRIMÁRIO:   descoberta e contato físico por BLE
FALLBACK 1: QR code contendo setup_code
FALLBACK 2: digitação manual do setup_code
```

Três termos passam a ter significados distintos e não intercambiáveis —
ver `CONTEXT.md` para as regras completas de cada um:

- **`setup_code`** — identificador humano de fabricação/registro inicial
  (12 dígitos numéricos aleatórios, tratado sempre como string). Não é
  credencial AWS, não é credencial permanente do dispositivo, e sozinho
  nunca conclui um claim.
- **`claim_session`** — autorização curta do backend, vinculada a um
  usuário autenticado + um dispositivo + uma tentativa específica de
  onboarding. Conceito ainda **não implementado** (Fase 3).
- **Fleet Provisioning temporary claim** — credencial temporária,
  específica da AWS (`CreateProvisioningClaim`), obtida pelo backend só
  depois de autorizar a aplicação, entregue ao dispositivo durante a
  sessão física de provisioning, e que nunca concede credenciais
  administrativas ao app. Também ainda **não implementado**.

Esta ADR é sobre arquitetura e nomenclatura, não sobre implementação:
nenhum banco de dados, endpoint de API, sessão de claim real, certificado
ou capacidade BLE foi criado por esta tarefa — ver `docs/phases.md`
(Fase 1B.2 = "arquitetura BLE-first" significa arquitetura **registrada**,
não BLE funcional no backend).

## Alternativas consideradas

1. **QR obrigatório.** Simples de especificar, mas exige que o QR esteja
   sempre acessível e legível, e não aproveita a presença física já
   confirmada por BLE quando o app já está perto do dispositivo. Rejeitada
   como único caminho por ser operacionalmente mais frágil sem ganho de
   segurança correspondente.
2. **Código manual obrigatório.** Evita depender de câmera, mas é a pior
   experiência de usuário das três (digitar 12 dígitos corretamente) e não
   resolve o problema de descoberta do dispositivo certo entre vários
   próximos. Rejeitada como único caminho pelo mesmo motivo.
3. **Claim apenas por `device_id`.** Foi descartada por razão de
   segurança, não de UX: o `device_id` (`ib-<32hex>`) não é secreto — ele
   pode aparecer em logs de rede, telemetria ou ser inferido — então
   permitir claim apenas com ele abriria caminho para *takeover* remoto de
   qualquer dispositivo ainda não reivindicado. Um segredo adicional
   (`setup_code`) e, no fluxo real, confirmação física (BLE) continuam
   obrigatórios.
4. **BLE-first com QR/manual como fallback (escolhida).** Combina a melhor
   experiência (descoberta automática de um dispositivo fisicamente
   próximo, sem digitação) com dois fallbacks de baixo custo de
   implementação para os casos em que BLE não está disponível (permissão
   negada, hardware incompatível, ambiente sem BLE). Nenhum dos três
   caminhos, sozinho, é suficiente para concluir a posse — todos convergem
   para o mesmo backend de autorização (`claim_session` +
   verificação cloud-side), então a superfície de ataque não aumenta por
   oferecer três entradas em vez de uma.

## Consequências positivas

- Onboarding mais rápido e resiliente a falhas de câmera/etiqueta na
  maioria dos casos (BLE primário).
- Terminologia sem ambiguidade (`setup_code` vs. `claim_session` vs. Fleet
  Provisioning temporary claim) reduz risco de erro de implementação
  futura — por exemplo, tratar um código de 12 dígitos como se fosse uma
  credencial permanente.
- A IoT Policy compartilhada já fica mais rígida agora
  (`iot:Connection.Thing.IsAttached`), independentemente de quando o BLE
  for de fato implementado — ver seção correspondente em `CONTEXT.md`.
- Nomes de regras de Basic Ingest e demais convenções de nomenclatura
  continuam centralizados em `infrastructure/config/iot.py`, então a
  mudança de fluxo de onboarding não exige alterações espalhadas pelo
  código quando a Fase 3 chegar.

## Riscos e trade-offs

- **Três caminhos de entrada em vez de um** aumentam a superfície de
  código a testar no app/firmware (fora do escopo deste repositório), mas
  não aumentam a superfície de autorização no backend, pois todos
  convergem para o mesmo `claim_session` (Fase 3).
- **Divergência temporária de documentação entre repositórios.** Esta ADR
  descreve uma arquitetura-alvo que ainda não existe em
  `interBridge/docs/communication-protocol.md` (que hoje ainda documenta
  `claim_code` e o QR antigo). Até a sincronização, um leitor que compare
  os dois repositórios verá dois vocabulários diferentes — mitigado
  registrando aqui, explicitamente, que o protocolo oficial ainda não foi
  atualizado (ver "Evolução histórica" abaixo).
- **Formato do QR ainda não é contrato oficial.** O formato conceitual
  `interbridge://claim?v=1&setup_code=<12 dígitos>` registrado aqui é uma
  proposta arquitetural deste backend, não um contrato ratificado pelo
  firmware — ver `docs/architecture.md`.
- **`setup_code` puramente numérico (12 dígitos) tem espaço de busca
  menor que um segredo alfanumérico longo.** Isso é mitigado pelo desenho
  (setup_code sozinho nunca conclui um claim; contato físico BLE ou posse
  do dispositivo continuam sendo exigidos) e por proteção contra abuso
  futura (rate limiting, cooldown — ver `CONTEXT.md`), não pela entropia
  do código em si.

## Impactos nos três repositórios

| Repositório | Impacto desta ADR |
| --- | --- |
| `interBridge` | Nenhuma alteração de código feita por esta tarefa. O firmware precisará eventualmente implementar descoberta/GATT BLE, expor `device_id` pelo canal BLE, gerar a chave privada permanente localmente, montar o CSR e falar Fleet Provisioning — nada disso foi tocado aqui. |
| `interapp` | Nenhuma alteração de código feita por esta tarefa. O app precisará eventualmente escanear/parear BLE, ler o `device_id`, oferecer QR/digitação manual como fallback, e chamar a futura API de claim — nada disso foi tocado aqui. |
| `interBackend` (este repositório) | Arquitetura, terminologia e endurecimento da IoT Policy registrados nesta tarefa. Nenhum banco, API, certificado ou capacidade BLE foi implementado. |

## Divisão de responsabilidades

- **BLE** é responsabilidade do firmware (`interBridge`) e do app
  (`interapp`) — descoberta, pareamento, transporte de credenciais
  temporárias e Wi-Fi. O `interBackend` nunca fala BLE diretamente.
- **`interBackend`** é responsável por: autorização, registry de
  dispositivos, ownership, claim sessions, integração futura com Fleet
  Provisioning, verificação cloud-side do provisioning, e auditoria/
  proteção contra abuso.
- A chave privada permanente do dispositivo **nunca** sai do ESP — nunca
  vai para o app, nunca vai para o backend, nunca vai para logs, nunca é
  commitada em nenhum dos três repositórios.

## Evolução histórica: do QR obrigatório ao BLE-first

1. **Fase 1A/1B.1:** o resumo de protocolo neste repositório refletia o
   protocolo oficial então vigente — QR como mecanismo primário de claim,
   usando o termo único `claim_code` tanto para o segredo de fabricação
   quanto (implicitamente) para qualquer autorização de sessão.
2. **Fase 1B.2 (esta ADR):** o backend adota preventivamente a
   arquitetura BLE-first e a nova terminologia (`setup_code`,
   `claim_session`, Fleet Provisioning temporary claim), antecipando a
   direção de produto, e endurece a IoT Policy compartilhada
   independentemente do onboarding (`iot:Connection.Thing.IsAttached`).
3. **Pendente (fases futuras):** quando `interBridge` e `interapp`
   publicarem suas próprias atualizações para o modelo BLE-first —
   inclusive uma nova revisão de
   `interBridge/docs/communication-protocol.md` com o formato de QR e o
   `setup_code` formalmente ratificados — este repositório deve revisar
   `CONTEXT.md`, este ADR e `docs/architecture.md` para reconciliar
   qualquer divergência de nome/formato que surgir. Até lá, o resumo de
   protocolo em `CONTEXT.md` continua citando o documento oficial atual
   (Draft v1.2, ainda baseado em `claim_code`) como fonte de verdade para
   o que já está implementado no firmware hoje.

## Decisões ainda abertas

- Formato definitivo e ratificado do QR (`setup_code` vs. outro nome que
  o firmware venha a escolher).
- Modelo definitivo das tabelas do Device Registry e da Claim Session (ver
  `CONTEXT.md`, seções correspondentes) — apenas os campos conceituais
  foram registrados, nenhum schema definitivo.
- Mecanismo exato de autenticação do usuário no app (Cognito é uma
  possibilidade, não decidida).
- Estratégia exata de rate limiting/cooldown contra abuso do
  `resolve-code` (limites por usuário, IP, `setup_code`, dispositivo —
  apenas os requisitos foram registrados).
- Momento exato da migração de associação não-exclusiva
  (`ClientId == ThingName`) para associação exclusiva
  (`ThingPrincipalType = EXCLUSIVE_THING`) no provisioning template
  futuro.
- Sinal(is) cloud-side exatos e sua combinação/prioridade para considerar
  o provisioning "verificado" (Thing registrado, certificado ativo,
  associação exclusiva, policy correta, Thing Group correto, conexão
  observada, estado esperado — a lista foi registrada, a lógica de
  combinação ainda não foi desenhada).
