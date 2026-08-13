# Deployment (futuro — não execute ainda)

**Nenhum dos comandos abaixo foi executado como parte da Fase 1A ou 1B.**
`cdk bootstrap` e `cdk deploy` criam ou alteram recursos reais na conta AWS
e **exigem autorização explícita** antes de serem executados.

Estas fases só executam validações locais e `cdk synth` (que apenas gera
templates CloudFormation localmente, sem tocar a conta AWS). O AWS IoT Core
foi apenas *verificado como acessível* na conta (`aws iot describe-endpoint`,
um comando somente leitura) — nenhum recurso foi criado.

## Pré-requisitos (quando o deploy for autorizado)

- Usuário IAM administrativo (ou role equivalente) configurado localmente
  via `aws configure` ou variáveis de ambiente — nunca o usuário root.
- MFA habilitado.
- Node.js/npm instalados para rodar o AWS CDK CLI (ver `README.md`).

## Etapas futuras (nesta ordem)

```bash
# 1. Confirmar a identidade/conta ativa (somente leitura, não altera nada)
aws sts get-caller-identity
aws configure get region

# 2. Sintetizar os templates (seguro, não toca a AWS)
npx aws-cdk@2 synth

# 3. Bootstrap do ambiente CDK (cria o toolkit stack; EXIGE AUTORIZAÇÃO)
npx aws-cdk@2 bootstrap aws://ACCOUNT_ID/sa-east-1

# 4. Revisar exatamente o que vai mudar antes de aplicar (seguro, somente leitura)
npx aws-cdk@2 diff

# 5. Aplicar as mudanças na conta AWS (EXIGE AUTORIZAÇÃO, cria recursos reais)
#    Nome real da stack de IoT, conforme sintetizado por `cdk synth`/`cdk list`:
npx aws-cdk@2 deploy InterBridge-Dev-IoTStack
```

`ACCOUNT_ID` acima é um placeholder — nunca substitua por um Account ID real
em um arquivo commitado neste repositório. Ao rodar esses comandos
localmente, use a variável de ambiente `CDK_DEFAULT_ACCOUNT` (lida
automaticamente pelo CDK CLI a partir das credenciais ativas) em vez de
digitar o Account ID em texto plano. Confirme o nome exato de cada stack com
`npx aws-cdk@2 list` antes de rodar `deploy` — os nomes vêm de
`infrastructure/config/naming.py::stack_id` e podem mudar se a configuração
mudar.

### O que o `cdk bootstrap` cria

`cdk bootstrap` cria uma stack chamada `CDKToolkit` na conta/região alvo.
Ela pode incluir, entre outros:

- um bucket S3 para armazenar assets de deploy (templates, código de Lambda
  quando existir);
- um repositório ECR (para imagens de container, caso algum dia sejam
  usadas);
- roles IAM usadas pelo próprio CDK para publicar assets e executar
  deploys;
- um parâmetro SSM com a versão do bootstrap.

Esses recursos **não pertencem funcionalmente ao InterBridge** — eles
sustentam o mecanismo de deploy do CDK em si, não a aplicação. Ainda assim,
podem gerar custo pequeno (armazenamento S3, etc.) e devem ser revisados
como qualquer outro recurso da conta — ver `docs/cost-controls.md`.

## Regras para esta fase (Fase 1A/1B)

- `cdk bootstrap`: **não autorizado** — nenhum comando desta lista foi
  executado nesta tarefa.
- `cdk deploy`: **não autorizado**.
- `cdk diff` contra uma conta real: **não autorizado** (exige bootstrap
  prévio e credenciais reais).
- `cdk synth`: **autorizado e esperado** — deve funcionar sem credenciais
  AWS reais (ver `README.md` e os testes em `tests/snapshot/` e
  `tests/unit/test_iot_stack.py`).
- O `diff` deve ser revisado manualmente, statement por statement da IoT
  Policy incluída, antes de qualquer deploy futuro ser autorizado.

## Antes de autorizar um deploy futuro

1. Ler `docs/cost-controls.md` e confirmar que nenhum recurso proibido foi
   adicionado às stacks.
2. Rodar `cdk diff` e revisar manualmente a lista de recursos.
3. Confirmar a região (`sa-east-1`) e o ambiente (`dev`) no output do
   `cdk diff`/`cdk deploy`.
4. Obter autorização explícita do responsável pelo projeto antes de rodar
   `cdk bootstrap` ou `cdk deploy`.
