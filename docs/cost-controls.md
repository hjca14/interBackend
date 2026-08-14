# Controle de custos

## Smoke test MQTT/mTLS

A preparação local da Fase 1D.1 não cria recursos nem gera custo AWS. Uma
execução futura autorizada usará um único Thing/certificado DEV e poucas
mensagens. Basic Ingest, processamento e persistência continuam fora desta
fase; nenhum recurso ou tópico diagnóstico temporário deve contornar isso.

Este documento resume os controles de custo em vigor e os cuidados a tomar
antes de qualquer deploy futuro. Nenhum valor exato de precificação é
inventado aqui — para números atualizados, consulte sempre a página oficial
de preços da AWS para a região `sa-east-1` antes de decidir sobre um deploy.

## Budget manual

- Um AWS Budget mensal de **US$ 10** já foi criado manualmente no console
  (fora deste repositório).
- Alertas configurados:
  - **US$ 1** de custo real acumulado no mês.
  - **US$ 5** de custo real acumulado no mês.
  - **US$ 10** de custo *previsto* (forecast) para o mês.
- **Importante:** um AWS Budget envia notificações (e-mail/SNS), mas **não
  é um bloqueio rígido** — ele não impede automaticamente a criação de
  recursos nem interrompe cobranças em andamento. Ele é um mecanismo de
  alerta, não de enforcement.

## Tags de custo

Todas as stacks aplicam as tags padrão (`Project`, `Environment`,
`ManagedBy`, `Repository`) e uma tag `Component` (`iot`, `api`, `database`,
`monitoring`) a cada stack — ver `infrastructure/config/environment.py`.
Essas tags permitem, no futuro, filtrar custos por componente no Cost
Explorer/relatórios de custo nativos da AWS.

Este projeto **não tenta reimplementar o AWS Cost Explorer** em um
frontend próprio — a visibilidade de custo é responsabilidade das
ferramentas nativas da AWS (Cost Explorer, Budgets, Cost and Usage Report).

## Recursos proibidos nesta fase (por causarem custo fixo/previsível)

Os seguintes recursos **não devem ser criados** nas fases iniciais deste
projeto, independentemente do uso real, pois têm custo fixo ou recorrente
mesmo em baixo tráfego:

- VPC com NAT Gateway (NAT Gateway cobra por hora, independente de uso).
- Instâncias EC2 sempre ligadas.
- Bancos de dados provisionados (RDS, Amazon OpenSearch Service) — usar
  apenas serviços serverless/pay-per-use (DynamoDB on-demand, Lambda, API
  Gateway HTTP API).
- Clusters Kubernetes (EKS) ou ECS com capacidade reservada.
- Qualquer serviço que exija provisionamento de capacidade mínima paga.

## Atenção especial (podem gerar custo mesmo sendo "serverless")

- **Logs do CloudWatch**: sem política de retenção, logs se acumulam
  indefinidamente. Toda `LogGroup` criada nas fases futuras deve declarar
  uma retenção explícita.
- **Dashboards do CloudWatch**: cada dashboard tem custo por dashboard/mês
  além de uma cota gratuita — mantenha o dashboard da `ObservabilityStack`
  pequeno (poucos widgets) e documente esse custo antes de criá-lo de fato.
- **Alarmes do CloudWatch**: cobrança por alarme além da cota gratuita.
- **DynamoDB**: usar billing mode on-demand (pay-per-request) em vez de
  capacidade provisionada, para evitar cobrança fixa em baixo uso.
- **Retenção de eventos/dados**: histórico de eventos de dispositivo sem
  TTL definido pode crescer indefinidamente em armazenamento.
- **AWS IoT Core**: cobrança por mensagem e por conexão — baixo custo em
  escala de protótipo/poucos dispositivos, mas deve ser monitorado à medida
  que o número de dispositivos cresce.

## Antes de cada deploy: revisar `cdk diff`

Antes de qualquer `cdk deploy` futuro (não autorizado nesta fase), sempre
rode `cdk diff` primeiro e revise manualmente:

- Quais recursos serão criados/alterados/removidos.
- Se algum recurso da lista de "recursos proibidos" acima apareceu por
  engano.
- Se alguma tabela DynamoDB, log group ou alarme está sem retenção/limite
  definido.

## Estimativa qualitativa de custos futuros

Sem inventar valores exatos (que mudam com o tempo e por região), a
expectativa qualitativa para as fases seguintes é:

- **Fase 1B/1D** (IoT Core mínimo, sem dispositivos reais em produção):
  custo esperado próximo de zero, dentro do Free Tier.
- **Fase 1C** (quatro tabelas DynamoDB on-demand implantadas em DEV,
  vazias, sem tráfego real — nenhuma Lambda/API ainda as usa): apenas
  armazenamento mínimo de tabelas vazias, esperado dentro do Free Tier —
  não garantido a exatamente zero.
- **Fase 1E/2 em diante** (Lambda + API Gateway + tráfego real nas
  tabelas da Fase 1C): custo esperado baixo, coberto majoritariamente
  pelo Free Tier nos primeiros 12 meses da conta, mas não garantido a
  zero.
- Consulte sempre <https://aws.amazon.com/pricing/> e a página de preços
  específica de cada serviço na região `sa-east-1` antes de deployar.

## Recursos da Fase 1B (IoT Thing Type, Thing Group, IoT Policy) — implantados na Fase 1B.3

- Um `AWS::IoT::ThingType`, um `AWS::IoT::ThingGroup` e um
  `AWS::IoT::Policy` **não geram tráfego nem custo por si só** — são apenas
  metadados/configuração; a AWS não cobra pela existência desses três
  recursos, somente por uso real do AWS IoT Core (conexões, mensagens,
  regras acionadas). Isso vale tanto para o template local (`cdk synth`)
  quanto para os três recursos já implantados em `dev`/`sa-east-1` desde
  a Fase 1B.3 — nenhum deles gera custo por existir; o Thing Group segue
  vazio (nenhum dispositivo conectando).
- O custo real do AWS IoT Core, quando um dispositivo existir (Fase 1D em
  diante), virá principalmente de:
  - tempo de conexão MQTT (cobrança por minuto de conexão, além de uma
    cota gratuita);
  - mensagens publicadas/entregues (cobrança por mensagem, além de uma
    cota gratuita);
  - execuções de regras de IoT (Basic Ingest), quando existirem (Fase 1E).
- Nenhum dashboard, alarme ou log detalhado do IoT Core foi criado — a
  policy e os recursos implantados não emitem métricas/logs adicionais
  por si próprios.
- O `cdk bootstrap` executado na Fase 1B.3 criou a stack `CDKToolkit`
  (bootstrap version 32) em `dev`/`sa-east-1`, que inclui armazenamento
  auxiliar (bucket S3 para assets de deploy) — ver `docs/deployment.md`.
  Esse armazenamento tem custo qualitativamente baixo em uso de
  protótipo, mas deve ser considerado ao revisar custos da conta como um
  todo.
- Reforçando: o Budget de US$ 10/mês **continua sendo um mecanismo de
  alerta, não um bloqueio automático** — ele não impede a criação de
  recursos nem interrompe cobranças; revisar `cdk diff` antes de cada
  deploy futuro continua sendo a defesa principal contra custo
  inesperado.

## Recursos da Fase 1C (DynamoDB) — implantados em DEV em 2026-08-13

- As quatro tabelas (`interbridge-dev-devices`,
  `interbridge-dev-setup-code-lookups`, `interbridge-dev-device-memberships`,
  `interbridge-dev-claim-sessions`) **agora existem em `dev`/`sa-east-1`**
  (`InterBridge-Dev-DataStack`, `CREATE_COMPLETE`) e usam billing on-demand
  (`PAY_PER_REQUEST`) — **não há custo fixo de capacidade provisionada**,
  apenas cobrança por requisição de leitura/escrita e por GB armazenado,
  ambos com cota gratuita mensal. Ver `docs/data-model.md` para o desenho
  completo.
- **DynamoDB não é incondicionalmente gratuito.** As quatro tabelas foram
  verificadas `ACTIVE` e **vazias** logo após o deploy — tabelas vazias
  devem ter custo muito baixo (armazenamento mínimo, sem requisições),
  mas **isso não é o mesmo que custo garantidamente zero**: mesmo em
  on-demand, armazenamento e requisições além da cota gratuita geram
  cobrança assim que houver tráfego real. Como nenhum consumidor em
  runtime existe ainda (nenhuma Lambda/API escreve nessas tabelas), o
  custo esperado até a Fase 1E/2 permanece baixo.
- Point-in-time recovery está **desativado** nas quatro tabelas (custo
  zero adicional; aceitável em DEV, sem dados de produção). Nenhum PITR,
  Stream, Global Table, chave KMS gerenciada pelo cliente, VPC, NAT
  Gateway ou Secrets Manager existe nesta fase.
- Nenhuma chave KMS gerenciada pelo cliente foi criada — criptografia usa
  a chave padrão da AWS, sem custo adicional de KMS.
- Nenhum DynamoDB Stream, nenhuma Global Table (replicação entre regiões)
  — ambos teriam custo adicional e não foram criados.
- `deletion_protection=True` + `RemovalPolicy.RETAIN` em todas as quatro
  tabelas, **agora em vigor na conta real**: isso não tem custo, mas
  significa que uma futura limpeza do DEV exigirá ações explícitas
  (desativar `deletion_protection` e excluir cada tabela manualmente) —
  `cdk destroy` sozinho não remove essas tabelas. Ver `docs/data-model.md`
  para o processo completo.
- O pepper do HMAC de `setup_code` **não** foi provisionado nesta fase —
  nenhum AWS Secrets Manager (que tem custo mensal por segredo) foi
  criado, deliberadamente, até existir um consumidor em runtime que o
  use.
