# Configuração da conta AWS

Este documento descreve o estado atual da conta AWS usada pelo projeto. Ele
não contém nenhum identificador real (Account ID, ARNs específicos, nomes de
usuário reais).

## Modelo de conta

- Uma única conta AWS é usada por enquanto (sem multi-conta).
- **Sem AWS Organizations.**
- **Sem IAM Identity Center** (antigo AWS SSO).
- Um usuário administrativo IAM já foi criado manualmente fora deste
  repositório, para uso no dia a dia (login via IAM, não via usuário root).
- **Nenhuma access key de usuário root** deve ser criada ou usada.
- Autenticação multifator (MFA) deve estar habilitada para o usuário IAM
  administrativo e, idealmente, para o usuário root também.

## Região

- Região padrão do projeto: `sa-east-1` (América do Sul — São Paulo).
- A infraestrutura CDK usa `sa-east-1` como padrão sempre que
  `CDK_DEFAULT_REGION` não estiver definido (ver `infrastructure/config/environment.py`).
- **Nuance do CDK CLI:** ao rodar `python app.py` diretamente, esse fallback
  para `sa-east-1` sempre se aplica quando `CDK_DEFAULT_REGION` está
  ausente. Ao rodar via `cdk synth`/`cdk deploy` (o CLI), é o próprio CLI
  quem define `CDK_DEFAULT_REGION` para o processo filho, resolvendo-o a
  partir do perfil/região AWS configurado localmente — e, na ausência de
  qualquer configuração, usando o fallback do próprio SDK da AWS
  (`us-east-1`), não o nosso. Configure um perfil AWS com região
  `sa-east-1` (recomendado para deploys futuros) ou exporte
  `AWS_REGION=sa-east-1` antes de rodar o CLI localmente sem perfil — ver
  `README.md`.

## Plano gratuito e orçamento

- O AWS Free Tier é preservado nesta fase — ver `docs/cost-controls.md` para
  detalhes de custo e os recursos evitados deliberadamente.
- Um AWS Budget mensal de US$ 10 já foi criado manualmente no console AWS
  (fora deste repositório, pois envolve configuração de conta, não código).

## Como verificar a identidade AWS (comandos somente leitura)

Estes comandos **não criam nem alteram nada** — servem apenas para
confirmar, no futuro, com qual identidade/conta você está autenticado antes
de rodar `cdk bootstrap` ou `cdk deploy`. Nenhum deles foi executado como
parte desta tarefa.

```bash
# Confirma qual identidade IAM está ativa na sessão local
aws sts get-caller-identity

# Lista os perfis configurados localmente (não expõe as credenciais)
aws configure list
```

Nunca cole a saída desses comandos (que inclui o Account ID real) em código,
commits, issues ou documentação deste repositório público.

## Boas práticas para este repositório público

- Nenhum Account ID real, ARN específico da conta, endpoint de IoT
  específico da conta, ou credencial deve aparecer em qualquer arquivo
  rastreado pelo git.
- Use sempre variáveis de ambiente (`CDK_DEFAULT_ACCOUNT`,
  `CDK_DEFAULT_REGION`) ou o perfil AWS CLI configurado localmente — nunca
  valores hardcoded.
- Veja `scripts/check_secrets.py` para a verificação local contra segredos.
