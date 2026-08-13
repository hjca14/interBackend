# Controle de custos

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

- **Fase 1B/1C** (IoT Core mínimo, sem dispositivos reais em produção):
  custo esperado próximo de zero, dentro do Free Tier.
- **Fase 1D em diante** (Lambda + API Gateway + DynamoDB com tráfego
  baixo): custo esperado baixo, coberto majoritariamente pelo Free Tier
  nos primeiros 12 meses da conta, mas não garantido a zero.
- Consulte sempre <https://aws.amazon.com/pricing/> e a página de preços
  específica de cada serviço na região `sa-east-1` antes de deployar.
