# Deployment (futuro — não execute ainda)

**Nenhum dos comandos abaixo foi executado como parte da Fase 1A.**
`cdk bootstrap` e `cdk deploy` criam ou alteram recursos reais na conta AWS
e **exigem autorização explícita** antes de serem executados.

Esta fase só executa validações locais e `cdk synth` (que apenas gera
templates CloudFormation localmente, sem tocar a conta AWS).

## Pré-requisitos (quando o deploy for autorizado)

- Usuário IAM administrativo (ou role equivalente) configurado localmente
  via `aws configure` ou variáveis de ambiente — nunca o usuário root.
- MFA habilitado.
- Node.js/npm instalados para rodar o AWS CDK CLI (ver `README.md`).

## Etapas futuras (nesta ordem)

```bash
# 1. Autenticar com o usuário/role administrativo (fora deste repositório)
aws sts get-caller-identity   # comando somente leitura, confirma a identidade ativa

# 2. Sintetizar os templates (seguro, não toca a AWS)
cdk synth

# 3. Bootstrap do ambiente CDK (cria o toolkit stack; EXIGE AUTORIZAÇÃO)
cdk bootstrap aws://ACCOUNT_ID/sa-east-1

# 4. Revisar exatamente o que vai mudar antes de aplicar (seguro, somente leitura)
cdk diff

# 5. Aplicar as mudanças na conta AWS (EXIGE AUTORIZAÇÃO, cria recursos reais)
cdk deploy
```

`ACCOUNT_ID` acima é um placeholder — nunca substitua por um Account ID real
em um arquivo commitado neste repositório. Ao rodar esses comandos
localmente, use a variável de ambiente `CDK_DEFAULT_ACCOUNT` (lida
automaticamente pelo CDK CLI a partir das credenciais ativas) em vez de
digitar o Account ID em texto plano.

## Regras para esta fase (Fase 1A)

- `cdk bootstrap`: **não autorizado**.
- `cdk deploy`: **não autorizado**.
- `cdk diff` contra uma conta real: **não autorizado** (exige bootstrap
  prévio e credenciais reais).
- `cdk synth`: **autorizado e esperado** — deve funcionar sem credenciais
  AWS reais (ver `README.md` e os testes em `tests/snapshot/`).

## Antes de autorizar um deploy futuro

1. Ler `docs/cost-controls.md` e confirmar que nenhum recurso proibido foi
   adicionado às stacks.
2. Rodar `cdk diff` e revisar manualmente a lista de recursos.
3. Confirmar a região (`sa-east-1`) e o ambiente (`dev`) no output do
   `cdk diff`/`cdk deploy`.
4. Obter autorização explícita do responsável pelo projeto antes de rodar
   `cdk bootstrap` ou `cdk deploy`.
