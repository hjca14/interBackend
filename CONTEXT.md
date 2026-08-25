# CONTEXT.md — interBackend

Este documento existe para que qualquer agente (humano ou IA) consiga
continuar o projeto **sem depender do histórico de nenhuma conversa
anterior**. Leia este arquivo e a documentação em `docs/` antes de editar
qualquer coisa.

## Identidade do projeto

Produto: **InterBridge** — um sistema de interfone/porteiro conectado.

Três repositórios compõem o produto:

| Repositório | Responsabilidade |
| --- | --- |
| [`interBridge`](https://github.com/hjca14/interBridge) | Firmware do dispositivo físico (ESP32). Dono do protocolo de comunicação dispositivo↔nuvem. |
| [`interapp`](https://github.com/hjca14/interapp) | Aplicativo Flutter usado pelo usuário final. Nunca se conecta diretamente ao broker MQTT. |
| [`interBackend`](https://github.com/hjca14/interBackend) (este repositório) | Backend e infraestrutura AWS: API HTTPS, Lambdas, DynamoDB, AWS IoT Core. |

**Regra importante:** as tarefas de Fase 1A, 1B.1, 1B.2, 1B.3 e 1C
trabalham exclusivamente no `interBackend`. Os outros dois repositórios
foram apenas consultados (somente leitura) para alinhamento e **não foram
alterados**.

## Decisões arquiteturais

- AWS como plataforma de nuvem, região inicial `sa-east-1` (São Paulo).
- AWS IoT Core como broker MQTT.
- MQTT/TLS entre dispositivo (`interBridge`) e AWS.
- Certificado X.509 individual por dispositivo (nunca gerado ou commitado
  neste repositório).
- API HTTPS (API Gateway + Lambda) entre `interapp` e o backend.
- O aplicativo **nunca** acessa o broker MQTT diretamente — sempre via API
  HTTPS.
- AWS IoT **Basic Ingest** para eventos do dispositivo, quando aplicável
  (evita round-trip por um tópico intermediário).
- DynamoDB como banco de dados planejado (modelo de tabelas ainda **não**
  fechado — ver "Pendências" abaixo).
- API Gateway (HTTP API) + Lambda como camada de backend planejada.
- Ambiente inicial: `dev`. Nenhum outro ambiente está configurado ainda.
- Uma única conta AWS por enquanto. Sem AWS Organizations. Sem IAM
  Identity Center. Plano gratuito (Free Tier) preservado deliberadamente.
- Infraestrutura gerenciada inteiramente por AWS CDK v2 (Python).
- Python usado tanto para o CDK quanto para as futuras Lambdas.
- Repositório **público** — nenhum segredo, certificado, chave privada ou
  dado pessoal pode ser commitado. Ver `scripts/check_secrets.py` e
  `.gitignore`.

## Contrato do protocolo v1 (resumo — não é a fonte oficial)

A fonte oficial e autoritativa do protocolo é
**`interBridge/docs/communication-protocol.md`**. Este resumo existe apenas
para orientação rápida de quem trabalha no backend e **nunca deve ser
copiado/expandido a ponto de competir com o documento oficial** — em caso
de dúvida ou divergência, o documento do `interBridge` prevalece.

- `protocol_version = 1` em toda mensagem custom.
- `command_id`: exatamente 32 caracteres hexadecimais minúsculos, sem
  prefixo.
- `issued_at` e `expires_at`: segundos Unix epoch (inteiros), **não**
  strings ISO-8601. O **backend** gera esses timestamps ao emitir um
  comando (é a autoridade sobre `issued_at`/`expires_at`); o dispositivo
  ainda valida a janela de validade e rejeita comandos expirados/inválidos
  de forma independente.
- Timestamp de evento (dispositivo → nuvem): string ISO-8601 UTC, ex.
  `2026-08-11T14:30:25Z`.
- Estados do intercomunicador: `IDLE`, `RINGING`, `OFF_HOOK`, `IN_CALL`,
  `ERROR`.
- Formato do QR code de reivindicação (claim):
  `interbridge://claim?v=1&device_id=ib-<32hex>&claim_code=<segredo>`.
- Comandos remotos proibidos continuam proibidos: `ENTER_PROVISIONING` e
  `FACTORY_RESET` exigem confirmação física no dispositivo e não são
  executáveis remotamente no protocolo v1.
- `claim_code` é um **segredo**, mas **não é uma credencial permanente** —
  é um segredo de reivindicação de posse do produto, distinto do
  certificado X.509 permanente do dispositivo.

**Importante (Fase 1B.2):** o resumo acima reflete a versão **atualmente
vigente** de `interBridge/docs/communication-protocol.md` (Draft v1.2),
que ainda usa `claim_code` e QR obrigatório. A partir da Fase 1B.2, este
backend adota preventivamente uma arquitetura BLE-first com terminologia
nova (`setup_code`, `claim_session`) — ver a seção "Onboarding BLE-first"
abaixo e `docs/adr/0001-ble-first-onboarding.md`. Essa nova terminologia
**ainda não existe** no documento oficial do firmware; até `interBridge`
publicar uma revisão do protocolo que a ratifique, trate-a como a direção
arquitetural planejada deste backend, não como um contrato já acordado
entre os três repositórios.

## Onboarding BLE-first (Fase 1B.2 — arquitetura e nomenclatura, não implementado)

Esta seção registra uma decisão arquitetural (ver ADR completo em
`docs/adr/0001-ble-first-onboarding.md`). **Nada nesta seção está
implementado**: nenhum BLE, banco de dados, endpoint de API, claim session
real ou Fleet Provisioning foi criado. É documentação para orientar as
fases futuras (Fase 3 em diante) sem repetir retrabalho arquitetural.

### Terminologia canônica (elimina a ambiguidade antiga de `claim_code`)

#### `setup_code`

Identificador humano criado no processo de fabricação ou registro
inicial.

- Formato inicial: **12 dígitos numéricos aleatórios**.
- Gerado criptograficamente; nunca sequencial.
- Zeros à esquerda são válidos; tratado sempre como **string**, nunca como
  número.
- **Não** é uma credencial AWS.
- **Não** é uma credencial permanente do dispositivo.
- **Não** transfere propriedade sozinho.
- Não pode ser listado ou pesquisado parcialmente.
- Não deve aparecer em logs.
- Armazenamento futuro será por representação protegida (ex.: hash),
  nunca texto aberto em listagens normais.

#### `claim_session`

Autorização curta do backend, vinculada a **usuário autenticado +
dispositivo + tentativa específica de onboarding**.

- Curta, de uso único, vinculada ao usuário, vinculada ao dispositivo,
  auditável, revogável.
- Incapaz de ser reutilizada como token genérico de provisioning.
- **Não implementada ainda** (Fase 3) — ver modelo conceitual abaixo.

#### Fleet Provisioning temporary claim

Credencial temporária emitida pelo **AWS IoT Fleet Provisioning by
Trusted User**.

- É um conceito específico da AWS — não é o `setup_code`, não é a
  `claim_session`.
- Expira em aproximadamente cinco minutos, conforme o fluxo padrão da
  AWS.
- Será obtida pelo backend somente **depois** da autorização da
  aplicação.
- Será entregue ao dispositivo durante a sessão física de provisioning
  (canal BLE).
- Nunca concede credenciais administrativas AWS ao aplicativo.
- **Não implementada nesta fase.**

Estes três termos **nunca devem ser usados de forma intercambiável** — são
três conceitos com donos, durações e garantias de segurança diferentes.

### Fluxo primário e fallbacks

```text
PRIMÁRIO:   descoberta e contato físico por BLE
FALLBACK 1: QR code contendo setup_code
FALLBACK 2: digitação manual do setup_code
```

Fluxo primário completo (planejado, não implementado):

```text
aplicativo autenticado
→ procura dispositivos próximos por BLE
→ usuário confirma o dispositivo físico
→ app obtém device_id pelo canal BLE
→ backend verifica se o device_id pode ser reivindicado
→ backend cria claim_session
→ app configura Wi-Fi e provisioning pelo canal BLE
→ backend autoriza temporary Fleet Provisioning claim
→ ESP provisiona credencial permanente
→ backend verifica o registro pelo lado da nuvem
→ somente então conclui a propriedade
```

Fluxo de fallback (QR e digitação manual transportam o **mesmo**
`setup_code` — não são fluxos de segurança diferentes entre si):

```text
setup_code
→ backend resolve device_id de forma protegida
→ app ainda precisa estabelecer contato BLE físico
→ claim_session
→ provisioning
→ verificação cloud-side
→ propriedade
```

Formato conceitual do QR (**proposto por este backend, ainda não
ratificado pelo `interBridge`** — ver nota acima):
`interbridge://claim?v=1&setup_code=<12 dígitos>`.

Regras:

- Possuir apenas o `setup_code` **nunca** conclui o claim.
- QR **não é obrigatório** no fluxo primário (BLE é primário).
- Digitação manual é fallback do QR, não um fluxo alternativo de
  segurança.
- Nenhum dos três caminhos (BLE, QR, manual) permite *takeover* remoto.
- O backend **não revela** se um código válido pertence a um dispositivo
  já registrado, inexistente, ou de outro usuário — erros públicos serão
  genéricos; detalhes apenas em logs seguros futuros.

### Responsabilidades

- **BLE** é responsabilidade do firmware (`interBridge`) e do app
  (`interapp`). O `interBackend` nunca fala BLE diretamente.
- **`interBackend`** é responsável por: autorização, registry de
  dispositivos, ownership, claim sessions, integração futura com Fleet
  Provisioning, verificação cloud-side, e auditoria/proteção contra
  abuso.

### Device Registry futuro (sem banco criado nesta fase)

Modelo futuro precisará representar, no mínimo:

```text
device_id
setup_code_lookup
hardware_version
ownership_status
aws_thing_name
provisioning_status
created_at
claimed_at
```

Requisitos registrados (sem tabela DynamoDB definitiva):

- `device_id` continua no formato `ib-<32 hex minúsculos>`.
- Identidade física do dispositivo é separada da propriedade.
- Ownership e memberships devem ser entidades separadas.
- Papéis futuros: `OWNER`, `ADMIN`, `MEMBER` — apenas `OWNER` precisa ser
  suportado inicialmente.
- Dispositivo já possuído não pode ser silenciosamente transferido.
- Identidade do proprietário atual nunca deve ser exposta em tentativa de
  claim (evita enumeração/engenharia social).
- Remoção, transferência e recuperação serão fluxos explícitos.
- O `setup_code` original não poderá ser reutilizado automaticamente para
  takeover.

### Claim session futura (modelo conceitual, sem schema definitivo)

```text
claim_session_id
device_id
user_id
created_at
expires_at
status
used_at
completed_at
```

Estados planejados:

```text
PENDING → AUTHORIZED → PROVISIONING → COMPLETED
                                    ↘ EXPIRED
                                    ↘ CANCELLED
                                    ↘ FAILED
```

Invariantes registradas:

- Somente o usuário criador utiliza a sessão.
- Somente o dispositivo vinculado pode ser provisionado nessa sessão.
- Expiração obrigatória.
- Conclusão idempotente.
- Cancelamento impede reutilização.
- `COMPLETED`, `EXPIRED` e `CANCELLED` são estados terminais.
- Propriedade só é concluída depois de confirmação confiável do lado da
  nuvem (nunca só pela afirmação do app).

### API futura (contrato preliminar — nenhum endpoint existe nesta fase)

```text
POST /devices/claim/resolve-code
POST /devices/claim/start
POST /devices/claim/complete
POST /devices/claim/cancel
```

- Rotas ainda podem mudar antes da Fase 3.
- Todas exigirão usuário autenticado.
- `resolve-code` não retorna informação sensível (sem confirmar
  posse/existência de forma que permita enumeração).
- `claim/start` aceita o contexto físico já resolvido (BLE ou
  `setup_code`).
- `claim/complete` **não confia apenas na afirmação do app** — exige sinal
  cloud-side (ver "Verificação de conclusão").
- `claim/cancel` invalida a sessão.

### Fleet Provisioning by Trusted User (decisão arquitetural, não implementada)

```text
Fleet Provisioning by Trusted User
+ CreateCertificateFromCsr
+ chave privada permanente gerada no ESP
```

Fluxo futuro:

1. Backend autentica e autoriza o usuário.
2. Backend cria/valida a `claim_session`.
3. Backend chama `CreateProvisioningClaim`.
4. Credencial temporária é transferida ao ESP pelo canal físico BLE.
5. ESP gera a chave privada permanente localmente.
6. ESP gera CSR.
7. ESP usa `CreateCertificateFromCsr`.
8. ESP chama `RegisterThing`.
9. AWS cria/associa Thing e certificado conforme o provisioning template.
10. ESP descarta a credencial temporária e reconecta com seu certificado
    permanente.
11. Backend verifica sinal cloud-side antes de concluir ownership.

A chave privada permanente:

- nunca sai do ESP;
- nunca vai para o aplicativo;
- nunca vai para o backend;
- nunca vai para logs;
- nunca é commitada.

Nenhum recurso de Fleet Provisioning foi criado por esta tarefa.

### Verificação de conclusão (cloud-side, idempotente)

O backend **não aceitará apenas** `provisioning_succeeded=true` enviado
pelo aplicativo. A verificação futura deve combinar sinais confiáveis do
lado AWS, tais como:

- Thing registrado com o nome esperado;
- certificado ativo;
- associação exclusiva certificado–Thing;
- policy correta;
- Thing Group correto;
- conexão autenticada observada;
- estado esperado do provisioning.

A implementação exata (quais sinais, em que combinação) ainda não foi
escolhida — só os requisitos (cloud-side, idempotente) foram registrados.

### Proteção contra abuso (requisitos futuros, rate limiting não implementado)

Requisitos futuros a projetar: limite por usuário, limite por IP, limite
por `setup_code` (representado de forma protegida, nunca em texto aberto),
limite por dispositivo, janela de tentativas, cooldown, auditoria,
detecção de brute force, respostas genéricas, prevenção de enumeração.

**Nunca registrar em log:** `setup_code` bruto, senha do Wi-Fi, chave
privada permanente, credencial temporária do Fleet Provisioning, access
key, secret key, session token.

Rate limiting **não foi implementado** nesta fase.

### Endurecimento da IoT Policy (Fase 1B.2 — implementado)

A `AWS::IoT::Policy` compartilhada (`interbridge-dev-device-policy`,
criada na Fase 1B.1) agora exige, em **todas as quatro statements**, a
condição oficial da AWS que garante que o certificado autenticado esteja
realmente anexado ao Thing avaliado:

```json
"Condition": {
  "Bool": {
    "iot:Connection.Thing.IsAttached": "true"
  }
}
```

- Confirmado contra a documentação oficial da AWS (operador `Bool`, chave
  `iot:Connection.Thing.IsAttached`, valor string `"true"`) — todo exemplo
  oficial de "registered device" a aplica no statement de `iot:Connect`.
- Decisão deste projeto: repetir a condição também em `Subscribe`,
  `Receive` e `Publish`, como defesa em profundidade — `IsAttached` é uma
  variável `Connection.*` como `${iot:Connection.Thing.ThingName}` (já
  usada nessas mesmas statements), e a AWS não documenta nenhuma restrição
  contra usá-la fora de `Connect`.
- **Modelo atual continua não-exclusivo:** o Client ID MQTT deve ser
  exatamente igual ao Thing Name (`${iot:Connection.Thing.ThingName}`
  resolve o nome a partir do Client ID quando a associação certificado↔
  Thing não é exclusiva). O provisioning template futuro deverá usar
  `ThingPrincipalType = EXCLUSIVE_THING`, `ThingName = device_id`,
  `ClientId = device_id` — e mesmo com associação exclusiva futura, o
  firmware continuará usando Client ID igual ao `device_id`.
- Nenhuma statement ganhou `iot:*` nem `Resource: "*"`; nenhum tópico de
  outro dispositivo passou a ser acessível. Ver
  `tests/unit/test_iot_stack.py` para os testes que travam essa garantia.

### Shadow e Jobs (capacidades futuras — nenhuma permissão concedida agora)

A policy permanente poderá futuramente receber acesso de privilégio
mínimo a Device Shadow e IoT Jobs do próprio Thing. Essas permissões só
serão adicionadas quando Shadow e Jobs forem efetivamente implementados e
testados — nenhum curinga foi concedido antecipadamente.

### Basic Ingest (decisão preservada da Fase 1B.1)

Basic Ingest continua sendo o mecanismo escolhido para `events`, `health`
e `command responses`. Os nomes das futuras regras continuam centralizados
em `infrastructure/config/iot.py` (`ingest_rule_name`,
`response_rule_name`). Nenhuma `AWS::IoT::TopicRule` ou downstream action
foi criada nesta fase. O firmware conhece apenas os nomes contratuais de
tópicos/regras necessários para publicar — não detalhes internos de
Lambda, DynamoDB ou outras implementações do backend.

## Estado atual (Fases 1A–1E e 2A concluídas; Fase 2B/2D implementadas localmente, não implantadas)

A Fase 2B declara Cognito, HTTP API/JWT Authorizer, os três GETs de dispositivos, o `PATCH` de
`display_name` (gerenciamento de dispositivos) e a ferramenta administrativa DEV; a Fase 2D
acrescenta as duas rotas assíncronas de comando (ver "Atualização — Fase 2D" abaixo). Seis rotas
JWT ao todo. Nenhum recurso, usuário ou dado foi criado na AWS; veja o runbook.

### Fase 2A — autenticação, autorização e contratos (somente documentação)

Em 2026-08-19 foram aceitos o ADR 0003, a arquitetura da Fase 2 e o OpenAPI `/v1` para
implementação futura. Cognito User Pool usará e-mail/senha verificado; `sub` será a identidade
canônica; o app terá somente HTTPS/JWT, sem Identity Pool; HTTP API/JWT Authorizer e membership
`ACTIVE` controlarão acesso. Ausência de dispositivo e ausência de membership serão indistinguíveis
por `404 RESOURCE_NOT_FOUND`. O registro do dispositivo DEV foi apenas desenhado como operação
interna, transacional e protegida. **Nenhum Cognito, API, Lambda, usuário, registro ou recurso AWS
foi criado; `ApiStack` permanece vazio.** Ver `docs/phase-2-architecture.md`,
`docs/openapi-v1.yaml` e `docs/adr/0003-phase-2-authentication-authorization.md`.

### Fase 1D.1 — preparação local do smoke test MQTT/mTLS

O pacote `mqtt_smoke/` e o runbook `docs/mqtt-smoke-test.md` estão
preparados localmente para um teste DEV controlado. O simulador representa
somente o dispositivo, usa MQTT 3.1.1/mTLS e sempre rejeita comandos sem
executar ações físicas. Com a Fase 1E implantada, health e respostas válidas são processados pelo Basic Ingest e persistidos na tabela separada de telemetria.

### Fase 1D.2 — ferramenta local do dispositivo DEV controlado

`tools/dev_iot_device.py` prepara operações `provision`, `verify` e `cleanup` para exatamente um
Thing/certificado MQTT/mTLS descartável em `dev`/`sa-east-1`. Ela exige STS e confirmação explícita,
valida os vínculos exatos e mantém certificado/chave/metadados fora do checkout. O runbook
autoritativo desta operação é `docs/phase-1d-dev-device.md`.

### Fase 1D.3/1D.4 — smoke real no simulador e no ESP32-C3

O simulador de computador foi validado primeiro, ponta a ponta, com um Thing DEV controlado e seu
certificado X.509 individual. Depois, o mesmo Thing/certificado foi usado em uma placa ESP32-C3
Super Mini genérica de bancada (chip ESP32-C3, 4 MB de flash e USB-C com USB nativa), com firmware
compilado pelo PlatformIO no ambiente compatível com `esp32-c3-devkitm-1`. Isso não define qual será
o módulo final da PCB comercial.

No hardware real foram validados: build e upload USB, inicialização, Wi-Fi 2,4 GHz, endpoint AWS IoT
Data ATS, porta 8883, MQTT/mTLS com Amazon Root CA 1, certificado exclusivo e chave correspondente
mantida somente no ambiente local do smoke DEV, `ClientId` igual/derivado do `device_id`, policy
vinculada ao Thing, assinatura QoS 1, health inicial QoS 0, comando `OPEN_DOOR` enviado pela AWS CLI
com JSON em Base64 no PowerShell, recebimento sem ação física e resposta segura com confirmação
serial `response publish: ok`. Após desligamento completo e novo boot, houve reconexão ao Wi-Fi e à
AWS, recebimento de novo comando e nova resposta segura.

Falhas DNS transitórias foram observadas. O endpoint foi validado externamente com registros A e
AAAA sem registrar hostname ou endereços, e o firmware conectou posteriormente pelas retentativas
existentes. O boot frio está validado; **queda e retorno do ponto de acesso enquanto o ESP32
permanece ligado não foram testados** e continuam pendentes.

**A Fase 1D está concluída somente neste escopo: “Primeiro dispositivo DEV controlado validado por
MQTT/mTLS no simulador e no ESP32-C3 real.”** Não foram validados onboarding BLE, Wi-Fi enviado pelo
app, NVS/NVS criptografada, chave privada gerada no dispositivo, CSR, Fleet Provisioning, Secure
Boot, Flash Encryption, OTA, hardware do interfone, GPIO/relé ou produção/fabricação. O cleanup ou decisão formal de retenção do Thing DEV continua pendente. A Fase 1E reutilizou esse dispositivo na validação real; a próxima fase é a Fase 2 — autenticação e API base.

### O que foi implementado — Fase 1A

- Estrutura completa do projeto CDK v2 em Python (`app.py`,
  `infrastructure/`, `tests/`, `docs/`, `.github/workflows/ci.yml`).
- Configuração tipada e centralizada (`infrastructure/config/`):
  `EnvironmentConfig`, `get_environment_config()`, `resource_name()`,
  `stack_id()`. Região padrão `sa-east-1`, ambiente padrão/único `dev`,
  Account ID nunca hardcoded (lido de `CDK_DEFAULT_ACCOUNT`).
- Quatro stacks (`DataStack`, `IoTStack`, `ApiStack`,
  `ObservabilityStack`), cada uma aplicando as tags padrão (`Project`,
  `Environment`, `ManagedBy`, `Repository`) e a tag `Component`
  correspondente (`database`, `iot`, `api`, `monitoring`).
- Verificação local de segredos (`scripts/check_secrets.py`), executada
  também em CI.
- `.gitignore` cobrindo certificados, chaves privadas, credenciais AWS,
  artefatos de provisioning, exports de tabelas, `.env`, `cdk.out/`, etc.
- `README.md`, `CONTEXT.md` e documentação em `docs/`.
- GitHub Actions (`ci.yml`) rodando lint, formatação, tipagem, testes,
  cobertura, `cdk synth` e verificação de segredos — sem acessar a conta
  AWS.

### O que foi implementado — Fase 1B

- `infrastructure/config/iot.py`: fonte única de verdade para nomes e
  tópicos de IoT — nomes determinísticos do Thing Type
  (`interbridge-dev-device`), Thing Group (`interbridge-dev-devices`) e
  IoT Policy (`interbridge-dev-device-policy`); construção dos tópicos do
  protocolo (`interbridge/{thing}/commands|events|health|responses`); e os
  nomes *reservados* (ainda não usados por nenhum recurso real) das
  futuras regras de Basic Ingest (`interbridge_dev_ingest_rule`,
  `interbridge_dev_response_rule` — com underscore, não hífen, porque
  `AWS::IoT::TopicRule` só aceita `[a-zA-Z0-9_]` no nome).
- `infrastructure/stacks/iot_stack.py` agora declara três recursos reais:
  - `AWS::IoT::ThingType` (`interbridge-dev-device`).
  - `AWS::IoT::ThingGroup` (`interbridge-dev-devices`), vazio — nenhum
    dispositivo foi adicionado.
  - `AWS::IoT::Policy` (`interbridge-dev-device-policy`) — policy
    compartilhada de privilégio mínimo com exatamente 4 statements
    (`iot:Connect`, `iot:Subscribe`, `iot:Receive`, `iot:Publish`), todas
    escopadas via `${iot:Connection.Thing.ThingName}` (nunca um device id
    fixo). Ver o resumo completo da policy mais abaixo.
- Outputs seguros (`CfnOutput`): nomes do Thing Type/Thing Group/Policy,
  região (pseudo-parâmetro `AWS::Region`) e ambiente. Nenhum output expõe
  Account ID, endpoint ou segredo.
- Testes semânticos extensos em `tests/unit/test_iot_stack.py` e
  `tests/unit/test_iot_naming.py` (contagem de recursos, nomes
  determinísticos, tags, cada statement da policy individualmente, ausência
  de `iot:*`/`Resource: "*"`, distinção `client/`·`topic/`·`topicfilter/`,
  preservação literal de `${iot:Connection.Thing.ThingName}`, ausência de
  Account ID/endpoint real).

### Resumo da IoT Policy (`interbridge-dev-device-policy`)

Quatro statements, todas `Effect: Allow`, nenhuma usa `iot:*` nem
`Resource: "*"`, todas com a condição `iot:Connection.Thing.IsAttached:
true` (endurecimento da Fase 1B.2 — ver seção "Onboarding BLE-first"
acima para o raciocínio completo):

1. `ConnectAsOwnThing` — `iot:Connect` em
   `client/${iot:Connection.Thing.ThingName}` (força MQTT Client ID = nome
   do Thing).
2. `SubscribeToOwnCommands` — `iot:Subscribe` em
   `topicfilter/interbridge/${iot:Connection.Thing.ThingName}/commands`.
3. `ReceiveOwnCommands` — `iot:Receive` em
   `topic/interbridge/${iot:Connection.Thing.ThingName}/commands`.
4. `PublishOwnEventsHealthAndResponses` — `iot:Publish` nos três caminhos
   de Basic Ingest: `topic/$aws/rules/interbridge_dev_ingest_rule/interbridge/${iot:Connection.Thing.ThingName}/events`,
   `.../health` (mesma regra), e
   `topic/$aws/rules/interbridge_dev_response_rule/interbridge/${iot:Connection.Thing.ThingName}/responses`.

A separação por dispositivo é garantida inteiramente pela variável nativa
`${iot:Connection.Thing.ThingName}`, resolvida pela AWS IoT Core no momento
da conexão a partir do Thing anexado ao certificado em uso — não por
lógica de aplicação. Isso é o motivo de a policy poder ser **compartilhada**
por todos os dispositivos com segurança. A condição `IsAttached` reforça
isso rejeitando qualquer certificado que não esteja de fato anexado a um
Thing registrado.

### O que foi implementado — Fase 1B.3 (implantação)

A Fase 1B.3 executou o primeiro `cdk bootstrap` e o primeiro `cdk deploy`
autorizados deste projeto, em `dev`/`sa-east-1`:

- **`CDKToolkit`** (stack de bootstrap do CDK): `CREATE_COMPLETE`,
  bootstrap version 32.
- **`cdk diff`** revisado manualmente antes do deploy, conforme exigido
  por `docs/deployment.md`.
- **`InterBridge-Dev-IoTStack`**: `CREATE_COMPLETE`. Os três recursos da
  Fase 1B.1/1B.2 agora existem de fato na conta AWS:
  - `AWS::IoT::ThingType` `interbridge-dev-device`.
  - `AWS::IoT::ThingGroup` `interbridge-dev-devices` — **ainda vazio**,
    nenhum dispositivo foi adicionado.
  - `AWS::IoT::Policy` `interbridge-dev-device-policy`, **versão 1** —
    exatamente uma policy, com as quatro statements endurecidas descritas
    acima.
- CI (`.github/workflows/ci.yml`) atualizada de Node.js 20 para **Node.js
  22** para o job que instala o AWS CDK CLI.
- Nenhum Account ID, ARN específico ou endpoint foi registrado neste
  repositório — ver "Regras para futuros agentes" abaixo.

**Importante:** esta implantação cobre apenas o que já estava sintetizado
nas Fases 1B.1/1B.2 (Thing Type, Thing Group vazio, IoT Policy). Nenhum
recurso novo foi criado durante o deploy além desses três — ver a
próxima seção para o que continua **não** implantado.

### O que foi implementado — Fase 1C (DynamoDB — implementado e implantado em DEV)

A `DataStack` declara quatro tabelas DynamoDB reais (ver
`docs/data-model.md` para o desenho completo). Implementação local
concluída, `cdk diff` revisado, e **deploy executado com sucesso**:

- **Data do deploy:** 2026-08-13. **Ambiente:** `dev`. **Região:**
  `sa-east-1`. **Stack:** `InterBridge-Dev-DataStack`. **Estado
  CloudFormation:** `CREATE_COMPLETE`.
- O CDK bootstrap (`CDKToolkit`, criado na Fase 1B.3) já existia e foi
  reutilizado — nenhum novo bootstrap foi necessário.
- O `cdk diff` foi revisado antes do deploy e continha **exatamente
  quatro novos recursos `AWS::DynamoDB::Table`** — nenhum recurso foi
  removido ou substituído, e **nenhuma alteração foi aplicada à
  `InterBridge-Dev-IoTStack`**.
- Após o deploy, as quatro tabelas foram verificadas via AWS CLI: todas
  `ACTIVE` e **vazias**. O TTL de `interbridge-dev-claim-sessions` foi
  confirmado `ENABLED` no atributo `ttl`.
- **Nenhum dado real foi inserido**: nenhum registro de dispositivo,
  `setup_code`, membership ou claim session. Nenhum Thing, certificado ou
  dispositivo real foi criado durante esta fase.

Tabelas implantadas:

- **`interbridge-dev-devices`** — partition key `device_id`. Registro de
  fabricação e status do dispositivo (`ownership_status`,
  `provisioning_status`).
- **`interbridge-dev-setup-code-lookups`** — partition key
  `setup_code_digest`. Resolve um `setup_code` para um `device_id` sem
  nunca armazenar o código em texto aberto — ver
  `domain/claims/setup_code.py` para o algoritmo `HMAC-SHA256(pepper,
  código)`.
- **`interbridge-dev-device-memberships`** — partition key `device_id`,
  sort key `user_id`, com um GSI `user_id`/`device_id` para listar os
  dispositivos de um usuário.
- **`interbridge-dev-claim-sessions`** — partition key
  `claim_session_id`, GSI `device_id`/`created_at`, TTL no atributo
  `ttl` (confirmado `ENABLED`).
- Configuração implantada nas quatro: billing `PAY_PER_REQUEST`,
  criptografia AWS-owned (sem KMS gerenciado pelo cliente), PITR
  desativado em DEV, `deletion_protection=True` + `RemovalPolicy.RETAIN`,
  sem Streams, sem Global Tables, sem role/policy IAM de runtime, sem
  Lambda, sem Cognito, sem API Gateway, sem VPC/NAT Gateway.
- `domain/devices/`, `domain/claims/`, `domain/ownership/`: modelos
  Python puros (sem `aws_cdk`, sem `boto3`) — `Device`, `SetupCodeLookup`,
  `ClaimSession`, `DeviceMembership`, seus enums, e as validações
  correspondentes (formato de `device_id`, normalização e digest do
  `setup_code`, consistência status/timestamp de `ClaimSession`). Esses
  modelos continuam sendo **código local** — nenhum serviço em runtime os
  usa ainda.
- O pepper do HMAC **não** foi provisionado — nenhum Secrets Manager,
  nenhuma chave KMS gerenciada pelo cliente — porque não existe
  consumidor em runtime ainda que o use. Ver `docs/data-model.md`.
- Nenhuma Lambda, API Gateway, Cognito, policy IAM, ou lógica de
  conclusão de claim foi criada — ver `docs/data-model.md` para os limites
  de IAM e a transação atômica futura documentados, não implementados.
- **Nenhum dado foi inserido manualmente nas tabelas para simular
  funcionalidades futuras** — as tabelas permanecem vazias, exatamente
  como um deploy legítimo de infraestrutura sem consumidor as deixaria.
- Testes: `tests/unit/test_data_stack.py` (infraestrutura) e
  `tests/unit/test_domain_devices.py`,
  `tests/unit/test_domain_setup_code.py`, `tests/unit/test_domain_claims.py`,
  `tests/unit/test_domain_ownership.py` (domínio).

### O que foi implementado e implantado — Fase 1E

Em 2026-08-18, no ambiente `dev`/`sa-east-1`, os deploys de
`InterBridge-Dev-DataStack`, `InterBridge-Dev-IngestionStack` e
`InterBridge-Dev-ObservabilityStack` foram concluídos com sucesso:

- A quinta tabela `interbridge-dev-telemetry` foi criada com DynamoDB on-demand e TTL
  `expires_at` nos registros temporários. As quatro tabelas anteriores da Fase 1C foram
  preservadas.
- A Lambda `interbridge-dev-ingestion-telemetry-handler`, duas Topic Rules de Basic Ingest, uma
  quarentena sanitizada e uma DLQ técnica foram criadas. Reserved concurrency foi removida em DEV
  porque o limite regional efetivo observado era 10, e a reserva impediria manter o mínimo de 10
  execuções não reservadas.
- As Topic Rules usam os aliases internos `ibmeta_device_id`, `ibmeta_category` e
  `ibmeta_received_at`, pois AWS IoT SQL rejeitou aliases iniciados por underscore. Os guards
  `isUndefined(...)` devem ser preservados: `WHERE` é avaliado contra o payload original antes de
  `SELECT`.
- Quatro alarmes CloudWatch foram criados: erros da Lambda, throttles da Lambda, mensagens
  visíveis na quarentena e mensagens visíveis na DLQ técnica.

A validação real com ESP32-C3 Super Mini confirmou MQTT/mTLS, assinatura de commands QoS 1, health
QoS 0 persistido, comandos publicados pelo AWS IoT MQTT Test Client recebidos pela placa e
responses QoS 1 persistidas. Foram observados registros `STATE#CURRENT`, `METRIC#...` e
`RESPONSE#...`, com `health_count=4`, `response_count=5` e `detailed_count=5` no período testado.
As cinco respostas estavam corretamente `REJECTED`/`COMMAND_EXPIRED`, pois os comandos `OPEN_DOOR`
foram publicados após a janela de validade de 10 segundos. Isso comprova AWS IoT commands → ESP32
→ responses → Basic Ingest → Lambda → DynamoDB. Não comprova ações físicas: o smoke firmware as
bloqueia propositalmente.

Continuam não validados: teste controlado da quarentena com payload inválido; teste controlado da
DLQ técnica; transição real dos alarmes para `ALARM`; perda e recuperação do access point com a
placa energizada; autenticação/API pública; BLE/Fleet Provisioning; ações físicas do interfone.
A próxima fase é a Fase 2 — autenticação e API base.

### Comandos que funcionam (validados localmente)

- `python -m venv .venv` + `pip install -r requirements.txt -r requirements-dev.txt`
- `pytest` — 244 testes, todos passando, 100% de cobertura em
  `infrastructure/` e `domain/`.
- `ruff check .` / `ruff format --check .`
- `mypy` (config em `pyproject.toml` cobre `infrastructure/` e `domain/`
  desde a Fase 1C; `mypy infrastructure domain` explicitamente também
  funciona).
- `python app.py` com `CDK_OUTDIR` customizado — sintetiza as 4 stacks sem
  credenciais AWS (`environment: aws://unknown-account/sa-east-1` no
  manifest).
- `AWS_REGION=sa-east-1 npx aws-cdk@2 synth` — CDK CLI instalado
  localmente via npm/`npx`. **Nota:** o próprio CDK CLI resolve
  `CDK_DEFAULT_REGION` a partir do SDK da AWS (não do nosso fallback
  Python) e, sem nenhum perfil configurado, cai para `us-east-1` — por
  isso `AWS_REGION=sa-east-1` (variável do SDK) deve ser exportada
  explicitamente ao rodar o CLI localmente sem perfil. Ver
  `docs/aws-setup.md` e `README.md`.
- `python scripts/check_secrets.py` — nenhum segredo encontrado.
- `cdk bootstrap`, `cdk deploy InterBridge-Dev-IoTStack` (Fase 1B.3) e
  `cdk deploy InterBridge-Dev-DataStack` (Fase 1C, 2026-08-13) —
  **executados com sucesso**, com credenciais reais e autorização
  explícita, fora do fluxo normal de CI (que nunca acessa a conta AWS).
  `CDKToolkit`, `InterBridge-Dev-IoTStack` e `InterBridge-Dev-DataStack`
  estão `CREATE_COMPLETE` em `dev`/`sa-east-1`. Qualquer novo `cdk deploy`
  (nessas ou em outra stack) deve, como sempre, ser precedido de
  `cdk diff` revisado e autorização explícita — ver `docs/deployment.md`.

Ver a seção "Relatório final" da tarefa que criou/atualizou este estado
(histórico de conversa) para os números exatos executados nesta rodada —
mas **não confie cegamente nisso**: rode os comandos acima novamente antes
de assumir que o estado ainda é válido.

### O que NÃO foi feito (fora do escopo concluído até a Fase 1E)

- Nenhuma access key ou credencial foi criada ou registrada no repositório.
- Nenhum Cognito, autenticação, API pública ou endpoint `/devices/claim/*` foi implementado.
- Nenhum BLE, Fleet Provisioning, `CreateProvisioningClaim`, `CreateCertificateFromCsr` ou
  `RegisterThing` foi implementado.
- O pepper do HMAC de `setup_code` ainda não foi provisionado.
- Os testes controlados de quarentena/DLQ e a transição real dos alarmes permanecem pendentes.
- Perda/recuperação do access point com a placa energizada e ações físicas do interfone não foram
  validadas.

## Fases planejadas

Ver `docs/phases.md` para critérios de conclusão detalhados de cada fase.

```text
Fase 1A   — fundação CDK                              [concluída]
Fase 1B.1 — base compartilhada do IoT                 [concluída e implantada]
Fase 1B.2 — arquitetura BLE-first                     [concluída]
Fase 1B.3 — bootstrap, diff e deploy mínimo           [concluída — CDKToolkit e IoTStack em dev/sa-east-1]
Fase 1C   — DynamoDB Device Registry/Ownership/Claim Sessions [concluída, implantada e validada em dev/sa-east-1]
Fase 1D   — primeiro dispositivo MQTT/mTLS            [concluída no escopo: simulador + ESP32-C3 real]
Fase 1E   — Basic Ingest, persistência real e observabilidade [concluída, implantada e validada em dev/sa-east-1]
Fase 2    — autenticação e API base                   [2A/2B/2D implementadas localmente, não implantadas]
Fase 3    — claim sessions (API), BLE-first e Fleet Provisioning [não iniciada]
Fase 4    — integração completa do interapp           [não iniciada]
Fase 5    — OTA, Jobs, escala e produção               [não iniciada]
```

**Nota de renumeração (Fase 1C):** as fases antigas "1C — primeiro
dispositivo MQTT/mTLS" e "1D — Basic Ingest..." foram renomeadas para
**1D** e **1E** para abrir espaço para a camada DynamoDB entre o deploy
mínimo de IoT (1B.3) e o primeiro dispositivo físico — ver a nota
equivalente em `docs/phases.md`. Nenhuma decisão de escopo mudou, só o
número.

**Nota:** "Fase 1B.2 — arquitetura BLE-first: código/docs prontos"
significa que a arquitetura, a terminologia e o endurecimento da IoT
Policy foram registrados/implementados — **não** que BLE está funcional
no backend. Nenhuma capacidade BLE existe em nenhum dos três repositórios.

## Pendências e decisões abertas

- **Pendência operacional — Node.js do AWS CloudShell:** o deploy da
  Fase 1C foi executado a partir do AWS CloudShell, cujo Node.js (v20)
  emitiu um aviso de depreciação durante a execução do CDK CLI. Isso
  **não** é uma falha do deploy (a `InterBridge-Dev-DataStack` concluiu
  `CREATE_COMPLETE` normalmente) e o aviso **não foi silenciado por
  variável de ambiente** — apenas registrado aqui. Antes das próximas
  fases que exigirem novo deploy, atualizar o ambiente de execução
  (CloudShell ou outro) para uma versão do Node.js igual ou compatível
  com a usada pela CI (Node.js 22 — ver `.github/workflows/ci.yml`).
- ~~**Modelo definitivo das tabelas DynamoDB**~~ — **resolvido na Fase
  1C**: quatro tabelas explícitas (`Devices`, `SetupCodeLookups`,
  `DeviceMemberships`, `ClaimSessions`), chaves e GSIs documentados em
  `docs/data-model.md`. Respostas MQTT usam `command_id` na chave da tabela separada de telemetria para idempotência; a emissão de comandos pela API permanece para a Fase 2.
- **Provisionamento do pepper do HMAC de `setup_code`**: mecanismo de
  segredo da AWS (Secrets Manager é o candidato mais provável) ainda não
  criado — ver `docs/data-model.md`.
- **Contratos exatos da API HTTPS** consumida pelo `interapp` (rotas,
  payloads, códigos de erro) — os quatro endpoints de claim têm nomes
  preliminares (ver "API futura" acima), mas nenhum contrato definitivo.
- **Autenticação do app**: mecanismo ainda não escolhido (Cognito é uma
  possibilidade, mas não foi decidido nem implementado).
- **Processo seguro de emissão de certificados** para os dispositivos.
- **Fleet Provisioning**: fluxo exato ainda não implementado.
- **Implementação da transação atômica de conclusão do claim**
  (`TransactWriteItems`) — o desenho está documentado em
  `docs/data-model.md`, nenhum código foi escrito.
- **Papéis IAM de privilégio mínimo** para os futuros consumidores das
  tabelas da Fase 1C (claim resolver, DeviceClaimService, membership
  reader, manufacturing importer) — documentados em `docs/data-model.md`,
  nenhuma role/policy criada.
- **Estratégia DEV/PROD futura**: hoje existe apenas `dev`; separação
  formal de ambientes/contas ainda não decidida.
- **Retenção de produção**: os valores DEV da Fase 1E (telemetria temporária por 30 dias, filas por quatro dias e logs por sete dias) devem ser revistos antes de produção.
- **Validação operacional pendente**: testes controlados da quarentena e DLQ técnica e transições reais dos alarmes para `ALARM`.
- **OTA** (atualização de firmware via AWS IoT Jobs): não implementado.
- **Domínio e identidade comercial** (nome de domínio, marca) ainda não
  definidos.
- **Eventual separação de contas AWS** (ex.: dev vs. prod) não decidida.
- **Revisão jurídica do nome "InterBridge"** ainda não realizada.
- **Formato definitivo e ratificado do QR** (`setup_code` vs. outro nome
  que o firmware venha a escolher) — ver
  `docs/adr/0001-ble-first-onboarding.md`.
- **Schema definitivo do Device Registry e da Claim Session** — apenas os
  campos conceituais foram registrados (ver "Onboarding BLE-first" acima).
- **Estratégia exata de rate limiting/cooldown** contra abuso do
  `resolve-code`.
- **Momento da migração** de associação não-exclusiva (`ClientId ==
  ThingName`) para associação exclusiva (`ThingPrincipalType =
  EXCLUSIVE_THING`) no provisioning template futuro.
- **Combinação exata de sinais cloud-side** para considerar um
  provisioning "verificado" (lista de sinais candidatos registrada, lógica
  de combinação ainda não desenhada).

## Regras para futuros agentes

1. Leia este `CONTEXT.md`, a documentação em `docs/` e os ADRs em
   `docs/adr/` antes de editar qualquer coisa.
2. Preserve o protocolo oficial: a fonte de verdade é sempre
   `interBridge/docs/communication-protocol.md`. Não duplique nem
   diverja dele neste repositório. Quando este `CONTEXT.md` descrever uma
   arquitetura-alvo (ex.: onboarding BLE-first) que ainda não existe no
   protocolo oficial, isso é sinalizado explicitamente — não trate como
   contrato já ratificado até o `interBridge` confirmar.
3. Não crie sucesso falso: nunca implemente endpoints, recursos ou testes
   que aparentem funcionar sem de fato funcionarem.
4. `cdk synth` local pode ser executado autonomamente (não acessa a conta
   AWS). `cdk diff` contra a conta real deve ser revisado manualmente
   antes de qualquer deploy. Qualquer deploy ou escrita na conta AWS
   (`cdk bootstrap`/`cdk deploy`/chamadas `aws` de escrita) exige
   autorização explícita e recente do responsável pelo projeto — mesmo
   que `CDKToolkit`, a `InterBridge-Dev-IoTStack` (Fase 1B.3) e a
   `InterBridge-Dev-DataStack` (Fase 1C) já estejam implantadas: isso
   autoriza o que já foi implantado, não mudanças futuras. Toda mudança
   nova, em qualquer stack, exige `cdk diff` revisado manualmente antes de
   um novo `cdk deploy`.
5. Nunca commite segredos, certificados, chaves privadas, Account IDs
   reais ou dados pessoais. Rode `python scripts/check_secrets.py` antes de
   commitar.
6. Não altere `interBridge` ou `interapp` sem solicitação explícita.
7. Atualize este `CONTEXT.md` depois de qualquer mudança arquitetural
   relevante.
8. Execute os testes (`pytest`, `ruff`, `mypy`, `cdk synth`) e relate os
   resultados honestamente — nunca invente resultados.
9. Preserve alterações do usuário: sempre rode `git status` antes de
   qualquer operação potencialmente destrutiva.
10. **Nunca insira dados manualmente nas tabelas DynamoDB implantadas**
    (`interbridge-dev-devices`, `interbridge-dev-setup-code-lookups`,
    `interbridge-dev-device-memberships`, `interbridge-dev-claim-sessions`)
    para simular funcionalidades futuras (registro de dispositivo, claim,
    membership). As tabelas devem permanecer vazias até que um serviço real das Fases 2/3 as escreva legitimamente; a ingestão da Fase 1E usa exclusivamente a quinta tabela de telemetria.

## Fase 1E — encerramento documental

A Fase 1E foi implementada, implantada e validada em DEV/`sa-east-1` em 2026-08-18. Este PR apenas
sincroniza a documentação com o estado real já observado; não executa chamadas AWS, deploys ou
alterações de infraestrutura. Consulte `docs/phase-1e-runbook.md` para resultados, limites e
validações ainda pendentes.

## Atualização — Fase 2D (2026-08-21)

A Fase 2C foi concluída e validada em DEV. A Fase 2D implementa localmente as duas rotas de comandos
assíncronos autenticados, persistência transacional de intenção/idempotência/cooldown na Telemetry e
publish interno de privilégio mínimo. Ainda não foi implantada; nenhum comando foi publicado e
nenhuma ação física foi testada. Nome personalizado por usuário permanece backlog futuro no
DeviceMembership (nota histórica desta data; ver "Atualização — gerenciamento de dispositivos"
abaixo para a decisão final: `display_name` por `Device`, não por membership). O estado operacional
e a ordem futura estão em `docs/phase-2d-runbook.md`.

### Fase 2D — capacidade OPEN_DOOR

O catálogo HTTP inicial contém somente `OPEN_DOOR`, como intenção semântica e sem qualquer detalhe
DTMF/GPIO/pulso. A configuração física futura pertence ao Device: `DISABLED` é o padrão seguro,
`DTMF` usa sequência local e `RELAY` futuro usa pulso local limitado; somente OWNER poderá alterá-la.
Nada disso foi implementado no firmware por este PR. Rejeições `NOT_CONFIGURED` e
`CAPABILITY_DISABLED` são públicas apenas como códigos sanitizados. `RESTART` não está exposto sem
caso de uso e política aprovados.

## Atualização — gerenciamento de dispositivos: listagem, detalhes e display_name (2026-08-25)

Primeira evolução do gerenciamento de dispositivos, implementada localmente (não implantada), sem
depender de hardware/firmware/BLE/MQTT em tempo real. Resolve, de forma diferente do apontado em
"Atualização — Fase 2D" acima, o nome personalizado do dispositivo: em vez de um campo por
`DeviceMembership` (por usuário), a decisão de produto desta revisão é um único `display_name`
opcional por `Device` (o produto modela um InterBridge por residência), sem qualquer campo de
cômodo/ambiente.

- `domain/devices/models.py` ganha `display_name: str | None` (trim, Unicode, 1-60 caracteres,
  compatível com itens antigos sem o atributo); validação isolada em
  `domain/devices/display_name.py`.
- `lambdas/read_api/handler.py`: `get_device` agora também retorna `created_at`/`updated_at`
  (RFC 3339) quando presentes no item; `list_devices`/`get_device` já retornavam `display_name`
  quando presente.
- Novo `lambdas/device_api/handler.py` (`update_device_name`): `PATCH /v1/devices/{device_id}`,
  somente `OWNER` ativo, corpo `{"display_name": "..."}` ou `{"display_name": null}` para limpar,
  `UpdateItem` com `SET`/`REMOVE` restrito a `display_name`/`updated_at` (nunca um `PutItem` de
  item inteiro) e `ConditionExpression="attribute_exists(device_id)"`.
- `infrastructure/stacks/api_stack.py`: sexta rota JWT (`UpdateDeviceNameFunction`), IAM mínimo
  (`dynamodb:GetItem` em `DeviceMemberships`, `dynamodb:UpdateItem` em `Devices`, nada mais).
- `docs/openapi-v1.yaml`: `PATCH /v1/devices/{device_id}` (`updateDeviceName`),
  `UpdateDeviceNameRequest`, e `created_at`/`updated_at` em `DeviceDetail`.
- **Pendente:** permissão de `ADMIN`/`MEMBER` para editar o nome (hoje só `OWNER`); nenhum deploy;
  nenhuma alteração no `interBridge` ou `interapp`.
