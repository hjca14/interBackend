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

**Regra importante:** as tarefas de Fase 1A e 1B trabalham exclusivamente no
`interBackend`. Os outros dois repositórios foram apenas consultados
(somente leitura) para alinhamento e **não foram alterados**.

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

## Estado atual (Fase 1A concluída; Fase 1B com código pronto, não implantado)

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
`Resource: "*"`:

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
por todos os dispositivos com segurança.

### O que é apenas estrutura / ainda não implantado (nenhum recurso AWS real)

- `DataStack`, `ApiStack` e `ObservabilityStack` **continuam sem nenhum
  recurso AWS** — apenas tags na própria stack. O modelo de dados da
  `DataStack` (DynamoDB) foi deliberadamente **não** criado antes de o
  modelo estar fechado (ver "Pendências" abaixo) — criar tabelas
  prematuramente arriscaria uma migração cara depois.
- `IoTStack` (Fase 1B) declara Thing Type/Thing Group/Policy no CDK, mas
  **nada foi implantado na AWS** — `cdk bootstrap` e `cdk deploy` não
  foram executados.
- Nenhum `AWS::IoT::Thing` individual, certificado X.509, chave privada,
  CSR, attachment ou provisioning template foi criado — isso é trabalho da
  Fase 1C, feito fora do Git.
- Nenhuma `AWS::IoT::TopicRule` (Basic Ingest) foi criada — apenas os
  *nomes* estão reservados na configuração, para Fase 1D.
- `lambdas/` não contém nenhuma função implementada.
- `infrastructure/constructs/` está vazio — nenhum padrão reutilizável
  foi necessário ainda.
- O AWS IoT Core foi **confirmado como acessível** na conta, na região
  `sa-east-1`, via `aws iot describe-endpoint --endpoint-type iot:Data-ATS
  --region sa-east-1` (comando somente leitura, executado fora deste
  repositório). O valor do endpoint retornado **não** foi registrado em
  nenhum arquivo deste repositório.

### Comandos que funcionam (validados localmente)

- `python -m venv .venv` + `pip install -r requirements.txt -r requirements-dev.txt`
- `pytest` — 77 testes, todos passando, 100% de cobertura em
  `infrastructure/`.
- `ruff check .` / `ruff format --check .`
- `mypy infrastructure`
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

Ver a seção "Relatório final" da tarefa que criou/atualizou este estado
(histórico de conversa) para os números exatos executados nesta rodada —
mas **não confie cegamente nisso**: rode os comandos acima novamente antes
de assumir que o estado ainda é válido.

### O que NÃO foi feito (deliberadamente, fora do escopo das Fases 1A/1B)

- `cdk bootstrap`: **não executado**.
- `cdk deploy`: **não executado**.
- `cdk diff` contra a conta real: **não executado**.
- Nenhum comando de escrita foi executado na conta AWS — apenas
  `aws iot describe-endpoint` (somente leitura, fora deste repositório).
- Nenhum recurso AWS real foi criado, alterado ou removido.
- Nenhuma access key foi criada.
- Nenhum GitHub OIDC configurado.
- Nenhum Cognito (ou outro provedor de autenticação) configurado.
- Nenhum certificado X.509, chave privada, CSR ou Thing individual
  gerado/criado.
- Nenhuma AWS IoT Rule (Basic Ingest) real criada.
- Nenhum scanner de QR ou cliente MQTT implementado no app.

## Fases planejadas

Ver `docs/phases.md` para critérios de conclusão detalhados de cada fase.

```text
Fase 1A — fundação do backend e CDK                 [concluída]
Fase 1B — infraestrutura mínima do AWS IoT Core     [código pronto; bootstrap/deploy pendentes]
Fase 1C — primeiro dispositivo MQTT/TLS             [não iniciada]
Fase 1D — ingestão, persistência e observabilidade  [não iniciada]
Fase 2  — autenticação do usuário e API do app      [não iniciada]
Fase 3  — claim por QR e provisioning                [não iniciada]
Fase 4  — integração completa do interapp            [não iniciada]
Fase 5  — fleet provisioning, OTA e produção          [não iniciada]
```

## Pendências e decisões abertas

- **Modelo definitivo das tabelas DynamoDB**: tabela única vs. múltiplas
  tabelas, chaves de partição/ordenação, GSIs, estratégia de idempotência
  de comandos.
- **Contratos exatos da API HTTPS** consumida pelo `interapp` (rotas,
  payloads, códigos de erro).
- **Autenticação do app**: mecanismo ainda não escolhido (Cognito é uma
  possibilidade, mas não foi decidido nem implementado).
- **Processo seguro de emissão de certificados** para os dispositivos.
- **Fleet Provisioning**: fluxo exato ainda não implementado.
- **Estratégia DEV/PROD futura**: hoje existe apenas `dev`; separação
  formal de ambientes/contas ainda não decidida.
- **Retenção de eventos e logs**: períodos de retenção ainda não
  definidos.
- **Limites e alarmes** operacionais (CloudWatch) ainda não definidos.
- **OTA** (atualização de firmware via AWS IoT Jobs): não implementado.
- **Domínio e identidade comercial** (nome de domínio, marca) ainda não
  definidos.
- **Eventual separação de contas AWS** (ex.: dev vs. prod) não decidida.
- **Revisão jurídica do nome "InterBridge"** ainda não realizada.

## Regras para futuros agentes

1. Leia este `CONTEXT.md` e a documentação em `docs/` antes de editar
   qualquer coisa.
2. Preserve o protocolo oficial: a fonte de verdade é sempre
   `interBridge/docs/communication-protocol.md`. Não duplique nem
   diverja dele neste repositório.
3. Não crie sucesso falso: nunca implemente endpoints, recursos ou testes
   que aparentem funcionar sem de fato funcionarem.
4. Não faça deploy (`cdk bootstrap`/`cdk deploy`) sem autorização explícita
   e recente do responsável pelo projeto.
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
