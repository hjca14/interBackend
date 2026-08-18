# Deployment

## Fase 1D.1 (sem deployment de stack)

O simulador e o runbook MQTT/mTLS são ferramentas locais. Prepará-los,
testá-los e executar `cdk synth` não cria Thing, certificado, attachment
ou regra IoT — nenhuma stack CDK (`IoTStack`, `DataStack` ou outra) foi
alterada ou reimplantada por essa validação. O provisionamento DEV manual
em `docs/mqtt-smoke-test.md` exige autorização separada; ele já foi
executado uma vez com sucesso via `tools/dev_iot_device.py` para um único
Thing/certificado DEV descartável (ver `docs/phase-1d-dev-device.md`), fora
de qualquer stack CDK e sem nenhum identificador real registrado aqui.

## Estado atual (Fases 1B.3, 1C e 1E concluídas)

Em DEV/`sa-east-1`, `CDKToolkit`, `InterBridge-Dev-IoTStack`,
`InterBridge-Dev-DataStack`, `InterBridge-Dev-IngestionStack` e
`InterBridge-Dev-ObservabilityStack` estão implantadas. Em 2026-08-18, a Fase 1E concluiu os
deploys da quinta tabela de telemetria (DynamoDB on-demand, TTL para itens temporários), da Lambda
de ingestão, duas Topic Rules Basic Ingest, quarentena sanitizada, DLQ técnica e quatro alarmes. As
quatro tabelas anteriores da Fase 1C foram preservadas.

A validação real com ESP32-C3 confirmou o fluxo de resposta até o DynamoDB. Não foram validados os
testes controlados de quarentena/DLQ, transições reais dos alarmes, ações físicas, BLE/Fleet
Provisioning ou autenticação/API pública. Nenhum Account ID, ARN completo, endpoint IoT ou segredo
é registrado neste repositório.

Isso não torna deploys futuros automáticos. Toda nova alteração continua exigindo diff, revisão e
autorização explícita.

## Pré-requisitos

- Usuário IAM administrativo (ou role equivalente) configurado localmente
  via `aws configure` ou variáveis de ambiente — nunca o usuário root.
- MFA habilitado.
- Node.js/npm instalados para rodar o AWS CDK CLI (ver `README.md`). A CI
  usa Node.js 22 (`.github/workflows/ci.yml`) — se o ambiente de deploy
  (ex.: AWS CloudShell) estiver em uma versão mais antiga e emitir aviso
  de depreciação, isso é uma pendência operacional a resolver antes do
  próximo deploy (ver "Pendências" em `CONTEXT.md`), não um motivo para
  abortar ou silenciar o aviso.

## Processo obrigatório para qualquer mudança futura

1. **Testes e synth locais** — `pytest`, `ruff`, `mypy`, `cdk synth` (sem
   credenciais AWS; pode ser executado autonomamente).
2. **`cdk diff` da stack específica** contra a conta real.
3. **Revisão manual do diff** — conferir exatamente quais recursos serão
   criados/alterados/removidos (ver "Antes de autorizar um novo deploy"
   abaixo).
4. **Autorização explícita** do responsável pelo projeto — renovada a
   cada deploy, mesmo em uma stack já implantada.
5. **Deploy** (`cdk deploy <stack>`).
6. **Validação pós-deploy** — confirmar o estado `CREATE_COMPLETE`/
   `UPDATE_COMPLETE` e, quando aplicável, verificar os recursos criados
   via AWS CLI (ex.: as quatro tabelas da Fase 1C foram confirmadas
   `ACTIVE`, vazias, com TTL `ENABLED`).
7. **Sincronização documental** — atualizar `README.md`, `CONTEXT.md`,
   `docs/phases.md` e os demais documentos relevantes com os fatos reais
   do deploy (é exatamente o que esta tarefa faz para a Fase 1C).

```bash
# 1. Confirmar a identidade/conta ativa (somente leitura, não altera nada)
aws sts get-caller-identity
aws configure get region

# Testes e síntese (seguro, não toca a AWS)
npx aws-cdk@2 synth

# Bootstrap -- já feito para dev/sa-east-1 (Fase 1B.3, reutilizado na
# Fase 1C). Só é necessário de novo em nova região/conta, ou se a AWS
# exigir uma versão mais nova do bootstrap.
npx aws-cdk@2 bootstrap aws://ACCOUNT_ID/sa-east-1

# 2-3. Diff da stack específica + revisão manual -- OBRIGATÓRIO antes de
#      qualquer deploy novo, mesmo em uma stack já implantada.
npx aws-cdk@2 diff InterBridge-Dev-DataStack

# 4-5. Aplicar as mudanças na conta AWS (EXIGE NOVA AUTORIZAÇÃO a cada vez)
npx aws-cdk@2 deploy InterBridge-Dev-DataStack
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
`dev`/`sa-east-1` (bootstrap version 32) — reutilizada pelas Fases 1C e 1E. Ela tipicamente inclui, entre outros:

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

- `cdk bootstrap`: já executado para `dev`/`sa-east-1` e reutilizado pela
  Fase 1C. Repetir só é necessário em nova região/conta ou se a AWS
  exigir uma versão mais nova.
- `cdk deploy`: **cada novo deploy exige nova autorização explícita** do
  responsável pelo projeto — a autorização de cada fase cobriu apenas o
  que foi de fato implantado naquele momento, não mudanças futuras.
- `cdk diff` contra a conta real: **obrigatório antes de qualquer novo
  deploy**, mesmo em uma stack já implantada (`IoTStack`, `DataStack`).
- `cdk synth`: continua funcionando sem credenciais AWS reais (ver
  `README.md` e os testes em `tests/snapshot/`, `tests/unit/test_iot_stack.py`
  e `tests/unit/test_data_stack.py`) e deve ser rodado antes do `diff`.
- O `diff` deve ser revisado manualmente — statement por statement no caso
  de mudanças na IoT Policy, tabela por tabela no caso de mudanças na
  `DataStack` — antes de qualquer deploy futuro ser autorizado.
- Após todo deploy, validar os recursos criados (via AWS CLI ou console)
  e sincronizar a documentação com os fatos reais observados — nunca
  inserir dados manualmente nas tabelas apenas para "simular" que uma
  fase futura já funciona.

## Antes de autorizar um novo deploy

1. Ler `docs/cost-controls.md` e confirmar que nenhum recurso proibido foi
   adicionado às stacks.
2. Rodar `cdk diff` e revisar manualmente a lista de recursos que vão
   mudar (criar/alterar/remover) — inclusive nas stacks já implantadas.
3. Confirmar a região (`sa-east-1`) e o ambiente (`dev`) no output do
   `cdk diff`/`cdk deploy`.
4. Obter autorização explícita do responsável pelo projeto antes de rodar
   `cdk bootstrap` (se necessário) ou `cdk deploy`.
5. Após o deploy, validar os recursos (estado `CREATE_COMPLETE`, dados
   ainda ausentes quando aplicável) e atualizar a documentação.

## Registro do deploy da Fase 1E

O deploy real de 2026-08-18 seguiu DataStack → IngestionStack → ObservabilityStack; rollback usa ordem inversa. O encerramento documental atual não executou chamadas AWS nem alterou infraestrutura. Consulte `docs/phase-1e-runbook.md` para os resultados reais e pendências.
