# Deployment

## Estado atual (Fase 1B.3 — concluída)

O primeiro bootstrap e o primeiro deploy deste projeto **já foram
executados**, com autorização explícita e credenciais reais, em
`dev`/`sa-east-1`:

- **`CDKToolkit`** (stack de bootstrap do CDK): `CREATE_COMPLETE`,
  bootstrap version 32.
- **`cdk diff`**: revisado manualmente antes do deploy.
- **`InterBridge-Dev-IoTStack`**: `CREATE_COMPLETE` — contém exatamente o
  que estava sintetizado nas Fases 1B.1/1B.2: um `AWS::IoT::ThingType`
  (`interbridge-dev-device`), um `AWS::IoT::ThingGroup`
  (`interbridge-dev-devices`, **ainda vazio**) e uma `AWS::IoT::Policy`
  (`interbridge-dev-device-policy`, versão 1, com as quatro statements
  endurecidas). Nenhum Thing individual, certificado, IoT Rule, Lambda,
  DynamoDB ou API foi criado.

Nenhum Account ID, ARN específico ou endpoint de IoT foi registrado neste
repositório — nem antes, nem depois do deploy. Ver `docs/aws-setup.md`.

**Isso não torna deploys futuros automáticos.** `DataStack`, `ApiStack` e
`ObservabilityStack` continuam sem nenhum recurso declarado, e qualquer
mudança nova — nessas stacks ou na própria `IoTStack` já implantada —
**exige o mesmo processo de novo**: `cdk diff` revisado manualmente e
autorização explícita antes de `cdk deploy`. O restante deste documento
descreve esse processo para as próximas mudanças.

## Pré-requisitos

- Usuário IAM administrativo (ou role equivalente) configurado localmente
  via `aws configure` ou variáveis de ambiente — nunca o usuário root.
- MFA habilitado.
- Node.js/npm instalados para rodar o AWS CDK CLI (ver `README.md`).

## Processo para qualquer mudança futura (nesta ordem)

```bash
# 1. Confirmar a identidade/conta ativa (somente leitura, não altera nada)
aws sts get-caller-identity
aws configure get region

# 2. Sintetizar os templates (seguro, não toca a AWS)
npx aws-cdk@2 synth

# 3. Bootstrap -- já feito para dev/sa-east-1 (Fase 1B.3). Só é necessário
#    de novo se a região/conta alvo mudar, ou se a AWS exigir uma versão
#    mais nova do bootstrap.
npx aws-cdk@2 bootstrap aws://ACCOUNT_ID/sa-east-1

# 4. Revisar exatamente o que vai mudar antes de aplicar (seguro, somente leitura)
#    OBRIGATÓRIO antes de qualquer deploy novo, mesmo em uma stack já implantada.
npx aws-cdk@2 diff

# 5. Aplicar as mudanças na conta AWS (EXIGE NOVA AUTORIZAÇÃO a cada vez)
npx aws-cdk@2 deploy InterBridge-Dev-IoTStack
```

`ACCOUNT_ID` acima é um placeholder — nunca substitua por um Account ID
real em um arquivo commitado neste repositório. Ao rodar esses comandos
localmente, use a variável de ambiente `CDK_DEFAULT_ACCOUNT` (lida
automaticamente pelo CDK CLI a partir das credenciais ativas) em vez de
digitar o Account ID em texto plano. Confirme o nome exato de cada stack
com `npx aws-cdk@2 list` antes de rodar `deploy` — os nomes vêm de
`infrastructure/config/naming.py::stack_id` e podem mudar se a
configuração mudar.

### O que o `cdk bootstrap` criou

O `cdk bootstrap` executado na Fase 1B.3 criou a stack `CDKToolkit` em
`dev`/`sa-east-1` (bootstrap version 32). Ela tipicamente inclui, entre
outros:

- um bucket S3 para armazenar assets de deploy (templates, código de
  Lambda quando existir);
- um repositório ECR (para imagens de container, caso algum dia sejam
  usadas);
- roles IAM usadas pelo próprio CDK para publicar assets e executar
  deploys;
- um parâmetro SSM com a versão do bootstrap.

Esses recursos **não pertencem funcionalmente ao InterBridge** — eles
sustentam o mecanismo de deploy do CDK em si, não a aplicação. Ainda
assim, podem gerar custo pequeno (armazenamento S3, etc.) e devem ser
revisados como qualquer outro recurso da conta — ver
`docs/cost-controls.md`.

## Regras para deploys futuros

- `cdk bootstrap`: já executado para `dev`/`sa-east-1`. Repetir só é
  necessário em nova região/conta ou se a AWS exigir uma versão mais
  nova.
- `cdk deploy`: **cada novo deploy exige nova autorização explícita** do
  responsável pelo projeto — a autorização da Fase 1B.3 cobriu apenas o
  que foi de fato implantado (Thing Type, Thing Group, IoT Policy), não
  mudanças futuras.
- `cdk diff` contra a conta real: **obrigatório antes de qualquer novo
  deploy**, mesmo em uma stack já implantada como a `IoTStack`.
- `cdk synth`: continua funcionando sem credenciais AWS reais (ver
  `README.md` e os testes em `tests/snapshot/` e
  `tests/unit/test_iot_stack.py`) e deve ser rodado antes do `diff`.
- O `diff` deve ser revisado manualmente, statement por statement no caso
  de mudanças na IoT Policy, antes de qualquer deploy futuro ser
  autorizado.

## Antes de autorizar um novo deploy

1. Ler `docs/cost-controls.md` e confirmar que nenhum recurso proibido foi
   adicionado às stacks.
2. Rodar `cdk diff` e revisar manualmente a lista de recursos que vão
   mudar (criar/alterar/remover) — inclusive na `IoTStack` já implantada.
3. Confirmar a região (`sa-east-1`) e o ambiente (`dev`) no output do
   `cdk diff`/`cdk deploy`.
4. Obter autorização explícita do responsável pelo projeto antes de rodar
   `cdk bootstrap` (se necessário) ou `cdk deploy`.
