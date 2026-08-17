# interBackend

> **Fase 1D concluída no escopo definido:** o primeiro dispositivo DEV controlado foi validado por
> MQTT/mTLS tanto no simulador de computador quanto em um ESP32-C3 real. Isso não valida onboarding
> BLE nem Fleet Provisioning de produção. A próxima fase de backend é a Fase 1E (Basic Ingest e
> persistência), que permanece pendente. Consulte
> [`docs/phase-1d-dev-device.md`](docs/phase-1d-dev-device.md).

Backend e infraestrutura AWS do **InterBridge** — um sistema de
interfone/porteiro conectado. Este repositório contém a infraestrutura como
código (AWS CDK v2, Python) e, nas fases futuras, as funções Lambda que
implementam a API usada pelo aplicativo.

## Relação com os outros repositórios

```text
interBridge  → firmware ESP32 (dono do protocolo de comunicação)
interapp     → aplicativo Flutter (nunca acessa o MQTT diretamente)
interBackend → este repositório: API HTTPS, Lambdas, DynamoDB, AWS IoT Core
```

- [`interBridge`](https://github.com/hjca14/interBridge) — a fonte oficial
  do protocolo de comunicação está em
  `interBridge/docs/communication-protocol.md`.
- [`interapp`](https://github.com/hjca14/interapp) — consome a API HTTPS
  exposta por este backend; nunca se conecta diretamente ao broker MQTT.

Ver `CONTEXT.md` para o histórico completo de decisões arquiteturais.

## Arquitetura (visão geral, planejada)

```text
interapp (Flutter) → HTTPS → API Gateway → Lambda → AWS IoT Core → MQTT/TLS → interBridge
```

Ver `docs/architecture.md` para o diagrama completo (incluindo o fluxo de
retorno de eventos) e para a distinção clara entre o que já está
implementado e o que é apenas planejamento.

## Estado atual da implementação

**Fase 1A (concluída):** fundação do projeto — estrutura CDK, configuração
tipada, quatro stacks preparatórias e a base de qualidade/CI.

**Fase 1B.1 (concluída e implantada):** a `IoTStack` declara, no CDK, a
infraestrutura compartilhada mínima do AWS IoT Core:

- **Thing Type** (`interbridge-dev-device`) — categoria de dispositivo
  InterBridge para o ambiente `dev`.
- **Thing Group** (`interbridge-dev-devices`) — grupo dos dispositivos de
  desenvolvimento; o Thing DEV controlado da Fase 1D foi adicionado fora do CDK.
- **IoT Policy** (`interbridge-dev-device-policy`, versão 1) — policy
  compartilhada de privilégio mínimo que qualquer certificado de
  dispositivo poderá usar no futuro; escopo por dispositivo via
  `${iot:Connection.Thing.ThingName}` (nunca um device id fixo).

**Fase 1B.2 (concluída):** adoção da arquitetura de onboarding
**BLE-first** (BLE como mecanismo primário de descoberta/claim; QR code e
digitação manual do `setup_code` como fallbacks equivalentes entre si) e
endurecimento da IoT Policy da Fase 1B.1 com a condição oficial da AWS
`iot:Connection.Thing.IsAttached: true` em todas as statements. Ver
[`docs/adr/0001-ble-first-onboarding.md`](docs/adr/0001-ble-first-onboarding.md)
para a decisão completa. Isso é arquitetura/nomenclatura e endurecimento
de policy — nenhum BLE, banco de dados, API ou Fleet Provisioning foi
implementado.

**Fase 1B.3 (concluída — implantado em `dev`/`sa-east-1`):** o
`CDKToolkit` (bootstrap do CDK) e a `InterBridge-Dev-IoTStack` foram
implantados com sucesso após revisão de `cdk diff`. Isso significa que o
**Thing Type, o Thing Group e a IoT Policy acima existem de fato na conta
AWS** (região `sa-east-1`, ambiente `dev`) — não são mais apenas
templates locais. Ver `docs/deployment.md` e `docs/phases.md` para
detalhes.

**Fase 1C (concluída e implantada):** a `DataStack` foi implantada com
sucesso em `dev`/`sa-east-1` em 2026-08-13 (`CREATE_COMPLETE`), após
`cdk diff` revisado — o diff continha exatamente quatro novos recursos
`AWS::DynamoDB::Table`, nada removido ou substituído, e **nenhuma
alteração na `InterBridge-Dev-IoTStack`**. As quatro tabelas —
`interbridge-dev-devices`, `interbridge-dev-setup-code-lookups`,
`interbridge-dev-device-memberships` e `interbridge-dev-claim-sessions` —
foram verificadas por AWS CLI após o deploy: todas `ACTIVE` e **vazias**,
com o TTL de `interbridge-dev-claim-sessions` confirmado `ENABLED` no
atributo `ttl`. Nenhum registro de dispositivo, `setup_code`, membership
ou claim session foi inserido. O pacote `domain/` traz os modelos Python
(independentes de `aws_cdk`/`boto3`) para dispositivo, membership e claim
session, incluindo o algoritmo de digest HMAC-SHA256 do `setup_code`. Ver
[`docs/data-model.md`](docs/data-model.md) para o desenho completo e
[`docs/deployment.md`](docs/deployment.md) para os fatos do deploy.

**O que ainda não existe:**

- Nenhuma função Lambda, API Gateway, dashboard ou alarme foi implantado
  (`ApiStack` e `ObservabilityStack` continuam sem nenhum recurso
  declarado).
- Não há autenticação, endpoints reais, ou regras de Basic Ingest
  (`AWS::IoT::TopicRule`) implantadas.
- **Fleet Provisioning de produção não existe.** Um único `AWS::IoT::Thing`
  DEV descartável e seu certificado X.509 exclusivo foram criados
  manualmente uma vez, fora do Git, apenas para o smoke test da Fase 1D
  (ver abaixo); isso não é Fleet Provisioning e não passa pelo registro de
  dispositivo do DynamoDB.
- **Nenhuma capacidade BLE existe** em nenhum dos três repositórios.
- **O backend funcional de onboarding (claim/provisioning) ainda não
  existe** — as tabelas estão implantadas e vazias, mas nenhum serviço as
  lê ou escreve ainda.

**Fase 1D (concluída no escopo definido):** `mqtt_smoke/` fornece o
simulador seguro já validado e o mesmo Thing DEV/certificado individual foi depois usado em uma
placa de bancada ESP32-C3 Super Mini genérica (chip ESP32-C3, flash de 4 MB, USB-C nativa), com
firmware PlatformIO compatível com `esp32-c3-devkitm-1`. O hardware real validou Wi-Fi 2,4 GHz,
MQTT/mTLS no endpoint Data ATS pela porta 8883, assinatura de comandos QoS 1, health inicial QoS 0,
recepção segura de `OPEN_DOOR`, resposta sem ação física e novo comando/resposta após desligamento
completo e novo boot. A placa é apenas a bancada validada, não uma definição do módulo da PCB
comercial. A queda e o retorno do ponto de acesso com o ESP32 ligado não foram testados. A próxima
fase de backend é a Fase 1E; Basic Ingest e persistência continuam pendentes.

**Mudanças futuras em qualquer stack (inclusive `IoTStack` e `DataStack`,
já implantadas) exigem `cdk diff` revisado e autorização explícita antes
de um novo `cdk deploy`** — ver `docs/deployment.md`.

Ver `CONTEXT.md` para o detalhamento completo do que existe vs. o que é
apenas estrutura, e `docs/phases.md` para as fases seguintes.

## Pré-requisitos

- Python 3.12 (preferencial) ou 3.11 — testado nesta máquina com Python
  3.11.9, que também é totalmente compatível com o AWS CDK v2.
- Node.js + npm — necessários apenas para rodar o **CDK CLI**, que é
  distribuído como pacote npm. Nenhuma dependência Node é usada pela
  aplicação Python em si.
- Git.

## Instalação local

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (Git Bash) / Linux / macOS
source .venv/Scripts/activate   # Git Bash no Windows
source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt
pip install -r requirements-dev.txt
```

O AWS CDK CLI é instalado via npm (não é uma dependência Python). Use-o via
`npx`, sem instalação global, para evitar poluir a máquina:

```bash
npx aws-cdk@2 --version
```

Copie `.env.example` para `.env` se quiser usar variáveis de ambiente
locais (o `.env` nunca é commitado — ver `.gitignore`).

## Comandos de desenvolvimento

```bash
# Testes
pytest

# Cobertura
pytest --cov

# Lint
ruff check .

# Formatação (verificação, sem alterar arquivos)
ruff format --check .

# Tipagem (configurada em pyproject.toml)
mypy infrastructure domain mqtt_smoke

# Verificação local de segredos
python scripts/check_secrets.py

# Síntese do CDK (não toca a conta AWS)
npx aws-cdk@2 synth
```

## Síntese do CDK (`cdk synth`)

`cdk synth` gera os templates CloudFormation localmente e **não requer
credenciais AWS reais**. O Account ID nunca é hardcoded (lido apenas de
`CDK_DEFAULT_ACCOUNT`, ficando ausente/agnóstico quando não definido) — isso
é validado em CI e em `tests/snapshot/test_app_synth.py`.

A região usada pela aplicação (`infrastructure/config/environment.py`) é
`sa-east-1` por padrão, sobrescrevível via `CDK_DEFAULT_REGION`. **Atenção**:
esse fallback só é aplicado quando você roda `python app.py` diretamente. Ao
rodar o comando `cdk synth` (via CLI), é o próprio CDK CLI quem calcula e
injeta `CDK_DEFAULT_REGION` para o processo da aplicação — em uma máquina
sem nenhum perfil/região AWS configurado, ele usa o fallback do próprio SDK
da AWS (`us-east-1`), não o nosso. Para garantir `sa-east-1` ao rodar
`cdk synth`/`npx cdk synth` localmente sem um perfil AWS configurado, exporte
a variável padrão do SDK antes:

```bash
export AWS_REGION=sa-east-1
npx aws-cdk@2 synth
```

(É assim que o workflow de CI garante `sa-east-1` de forma determinística —
ver `.github/workflows/ci.yml`.)

## Região

Região padrão: **`sa-east-1`** (América do Sul — São Paulo).

## Política de segredos

Este repositório é **público**. Nunca commite:

- Account ID real, access keys, secret keys, session tokens;
- certificados X.509 ou chaves privadas de dispositivos;
- claim codes reais, endpoints de IoT específicos da conta;
- dados pessoais (e-mail, telefone, IDs reais de dispositivo).

O `.gitignore` bloqueia os padrões mais comuns (`.env`, `*.pem`, `*.key`,
`*.crt`, `cdk.out/`, etc.) e `scripts/check_secrets.py` faz uma varredura
local adicional, executada também em CI. Ver `docs/aws-setup.md` e
`docs/cost-controls.md` para mais detalhes sobre a conta AWS e custos.

## ⚠️ Novos `cdk deploy` exigem `cdk diff` revisado e autorização

O `CDKToolkit` (bootstrap), a `InterBridge-Dev-IoTStack` (Fase 1B.3) e a
`InterBridge-Dev-DataStack` (Fase 1C, 2026-08-13) já foram implantados em
`dev`/`sa-east-1`. Isso **não** torna deploys futuros automáticos:
qualquer mudança nova — nessas stacks ou em qualquer outra (`ApiStack`,
`ObservabilityStack`) — ainda exige rodar `cdk diff`, revisar manualmente
o que vai mudar, e obter autorização explícita antes de `cdk deploy`. Ver
`docs/deployment.md`.

## Documentação

- [`CONTEXT.md`](CONTEXT.md) — contexto completo do projeto para agentes futuros.
- [`docs/architecture.md`](docs/architecture.md) — arquitetura e diagramas.
- [`docs/aws-setup.md`](docs/aws-setup.md) — configuração da conta AWS.
- [`docs/cost-controls.md`](docs/cost-controls.md) — controle de custos e budget.
- [`docs/deployment.md`](docs/deployment.md) — processo de deploy: o que já foi executado (Fases 1B.3 e 1C) e o que ainda exige autorização.
- [`docs/phases.md`](docs/phases.md) — fases planejadas do projeto.
- [`docs/adr/0001-ble-first-onboarding.md`](docs/adr/0001-ble-first-onboarding.md) — decisão arquitetural do onboarding BLE-first.
- [`docs/data-model.md`](docs/data-model.md) — desenho das tabelas DynamoDB (implantadas em DEV) e dos modelos de domínio.

## Fase 1E (estado deste PR)

AWS IoT Basic Ingest, a tabela operacional de telemetria, a Lambda de ingestão, quarentena sanitizada, DLQ técnica e quatro
alarmes estão **implementados e testados localmente, mas ainda não foram implantados nem validados
na AWS**. O contrato do firmware, os tópicos/QoS e a policy compartilhada do dispositivo não foram
alterados. Consulte `docs/phase-1e-runbook.md` antes de qualquer operação autorizada.
