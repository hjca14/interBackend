# Data model (Fase 1C)

Este documento descreve o modelo de persistência DynamoDB do `interBackend`:
as quatro tabelas declaradas em `infrastructure/stacks/data_stack.py`, os
modelos de domínio equivalentes em `domain/`, e os padrões de acesso que
eles foram desenhados para suportar.

**Estado desta fase: implementado localmente, ainda não implantado.**
`cdk synth` produz as quatro tabelas com sucesso, mas nenhum `cdk deploy`
foi executado — ver `docs/deployment.md`. Não existe nenhum consumidor em
runtime (Lambda, API) ainda; `domain/` é código Python puro, independente
de `aws_cdk` e de `boto3`, pronto para ser usado quando um consumidor for
implementado.

## Visão geral das quatro tabelas

| Tabela | Partition key | Sort key | GSI | TTL |
| --- | --- | --- | --- | --- |
| `interbridge-dev-devices` | `device_id` | — | — | — |
| `interbridge-dev-setup-code-lookups` | `setup_code_digest` | — | — | — |
| `interbridge-dev-device-memberships` | `device_id` | `user_id` | `*-by-user-index` (`user_id` / `device_id`) | — |
| `interbridge-dev-claim-sessions` | `claim_session_id` | — | `*-by-device-index` (`device_id` / `created_at`) | `ttl` |

Todas as quatro:

- billing on-demand (`PAY_PER_REQUEST`) — sem capacidade provisionada, sem
  autoscaling;
- criptografia com chave pertencente à AWS (`TableEncryption.DEFAULT` no
  CDK, que renderiza `SSESpecification.SSEEnabled=false` no CloudFormation
  — o comportamento clássico de "chave da AWS", distinto do recurso
  opcional de SSE com KMS gerenciado pelo cliente ou pela AWS); **nenhuma**
  chave KMS gerenciada pelo cliente é usada;
- point-in-time recovery **desativado** (aceitável em DEV, onde as
  tabelas ainda não guardam dados que valham a pena restaurar — revisar
  antes de um lançamento real);
- `deletion_protection=True` **e** `RemovalPolicy.RETAIN`;
- sem DynamoDB Streams, sem Global Tables (`replication_regions`), sem
  dado semente (seed) ou dispositivo de exemplo;
- tags padrão do projeto (`Project`, `Environment`, `ManagedBy`,
  `Repository`) + `Component=database`.

Nomes centralizados em `infrastructure/config/data.py` — assim como
`infrastructure/config/iot.py` fez para os recursos de IoT na Fase 1B, os
nomes exatos pedidos (`interbridge-dev-devices`, etc., sem o segmento de
`component` que o helper genérico `resource_name()` sempre insere) exigiram
um módulo de nomenclatura dedicado, em vez de forçar o helper genérico a um
caso especial.

### Efeito de `RemovalPolicy.RETAIN` + `deletion_protection` na limpeza do DEV

Isso é uma escolha deliberada, não um esquecimento: **nenhuma tabela pode
ser removida por `cdk destroy` nem por acidente no console.** Para
desmontar o ambiente `dev` no futuro (ex.: recriar do zero), será
necessário, para cada tabela, **manualmente**:

1. Desativar `deletion_protection` (via console ou `aws dynamodb
   update-table`).
2. Excluir a tabela explicitamente.

`cdk destroy`/remover a tabela do código **não** apaga os dados sozinho.
Essa fricção é intencional: evita perda acidental de dados de dispositivos
e claim sessions reais no futuro.

## Access patterns

| Operação | Mecanismo |
| --- | --- |
| Obter dispositivo por `device_id` | `GetItem` na tabela `Devices` |
| Resolver `setup_code` exato | `HMAC-SHA256(pepper, code)` + `GetItem` em `SetupCodeLookups` (leitura fortemente consistente — ver nota abaixo) |
| Obter membros de um dispositivo | `Query` em `DeviceMemberships` por `device_id` |
| Listar dispositivos de um usuário | `Query` no GSI `user_id`/`device_id` |
| Obter claim session por ID | `GetItem` em `ClaimSessions` |
| Obter claims recentes de um dispositivo | `Query` no GSI `device_id`/`created_at` |

**Leitura fortemente consistente em `SetupCodeLookups`:** DynamoDB só
oferece leitura fortemente consistente em `GetItem`/`Query` contra a
tabela base (não contra um GSI). Como a resolução de `setup_code` usa
`GetItem` diretamente na tabela `SetupCodeLookups` (não um índice), basta
passar `ConsistentRead=True` na chamada futura — nenhuma configuração
adicional de infraestrutura é necessária.

### Explicitamente proibido

- `Scan` para resolver `setup_code` — sempre `GetItem` pelo digest.
- Pesquisa parcial de `setup_code` (nenhum índice a permite).
- Listagem de setup codes.
- Enumeração do proprietário atual de um dispositivo em resposta a uma
  tentativa de claim.
- Acesso direto do app móvel ao DynamoDB (sempre via uma futura API —
  Fase 2/3).
- Armazenamento de `setup_code` em texto aberto.
- Armazenamento de senha de Wi-Fi.
- Armazenamento da chave privada permanente do dispositivo.
- Armazenamento da credencial temporária do Fleet Provisioning.

Nenhum desses veta a leitura administrativa via console/CLI para
depuração — vetam apenas os *access patterns* da aplicação.

### Por que nenhum GSI particionado só por `status`

Um GSI cuja partition key fosse apenas `status` (ex.: todas as sessões
`PENDING`) teria cardinalidade baixíssima — todas as sessões pendentes do
sistema inteiro cairiam na mesma partição do índice, criando um hot key.
O GSI de `ClaimSessions` implementado é particionado por `device_id` (alta
cardinalidade), com `created_at` como sort key.

## `setup_code`: nunca armazenado em texto aberto

Ver `domain/claims/setup_code.py`. Algoritmo:

1. `normalized_code`: exatamente 12 dígitos ASCII (`0`-`9`); dígitos
   Unicode de outros scripts (ex.: arábico-índico, largura total) são
   rejeitados mesmo que `str.isdigit()` os aceite, porque a validação usa
   a classe de caracteres `[0-9]` (que só casa os pontos de código ASCII
   literais), não `\d`.
2. `setup_code_digest = HMAC-SHA256(pepper, normalized_code)` — 64
   caracteres hexadecimais minúsculos.

Um SHA-256 simples do código de 12 dígitos **não seria aceitável**: o
espaço de entrada é de apenas 10¹² valores (~40 bits) — pequeno o
suficiente para um invasor com uma cópia roubada da tabela de lookup
gerar e comparar todos os hashes possíveis offline. Uma HMAC com uma
chave secreta por implantação (o "pepper") inviabiliza esse ataque de
força bruta offline sem também roubar o pepper.

### Por que o pepper não é provisionado nesta fase

Não existe nenhum consumidor em runtime ainda — nenhuma Lambda, nenhuma
API. Provisionar um AWS Secrets Manager ou uma chave KMS gerenciada pelo
cliente agora geraria custo recorrente e superfície operacional antes de
haver qualquer código que efetivamente use o segredo. O pepper será
mantido em um mecanismo de segredo da AWS (Secrets Manager é o candidato
mais provável) em uma fase futura de runtime, quando o primeiro
consumidor for implementado — ver "Pendências" em `CONTEXT.md`.

O helper `compute_setup_code_digest()` reflete essa decisão no código: o
parâmetro `pepper` é obrigatório, **sem valor padrão, sem fallback via
variável de ambiente, sem hardcode** — e o módulo não importa `logging`,
então nem o código nem o pepper podem vazar em log por engano.

## TTL em `ClaimSessions`

O atributo `ttl` (Unix epoch segundos) habilita a expiração automática do
DynamoDB, e é validado (`domain/claims/models.py`) para ser sempre igual a
`expires_at`.

**A exclusão por TTL é assíncrona** — a AWS documenta que pode levar até
48 horas após o timestamp de expiração para de fato remover o item. Isso
**não é um mecanismo de autorização**: o runtime futuro sempre deve
comparar `expires_at` com o relógio autoritativo do próprio backend em
tempo de leitura, e tratar uma sessão como expirada assim que
`expires_at` for ultrapassado — independentemente de o item já ter sido
fisicamente removido pelo TTL ou não.

## Conclusão atômica futura do claim (não implementada)

Documentado como decisão arquitetural para a Fase 3. A transação
(`TransactWriteItems`) deverá:

1. Verificar que a claim session não expirou.
2. Verificar que pertence ao usuário autenticado e ao dispositivo correto.
3. Verificar que ainda não foi utilizada.
4. Verificar evidência confiável de provisionamento na AWS (ver
   `CONTEXT.md`, "Verificação de conclusão").
5. Alterar condicionalmente o dispositivo de `UNCLAIMED`/`CLAIM_IN_PROGRESS`
   para `OWNED`.
6. Criar a associação `OWNER` em `DeviceMemberships`.
7. Marcar a sessão como `COMPLETED` (terminal, single-use).
8. Falhar atomicamente se qualquer condição não for satisfeita — nenhuma
   escrita parcial.

Nenhum serviço (real ou stub) que implemente isso foi criado nesta fase.

## `DeviceMemberships`: por que o único-`OWNER` não é garantido pelo DynamoDB

O requisito de produto é que apenas um `OWNER` ativo exista por
dispositivo neste momento (`ADMIN`/`MEMBER` reservados para o futuro). O
DynamoDB não tem um jeito nativo de expressar "no máximo um item com
`role=OWNER` para esta partition key" — uma verificação de item único não
enxerga outros itens da mesma partição. Isso terá que ser implementado
por um futuro serviço, tipicamente com uma escrita condicional contra um
atributo denormalizado no item `Device` (ex.: `owner_user_id`). Nenhuma
lógica de aplicação foi implementada nesta fase; apenas o modelo de dados
que a sustenta.

## IAM: limites mínimos futuros (nenhuma policy criada nesta fase)

Nenhuma role/policy IAM foi criada — não há consumidor em runtime ainda.
Os limites mínimos documentados para quando houver:

| Papel futuro | Permissão mínima |
| --- | --- |
| `claim resolver` | `dynamodb:GetItem` somente em `SetupCodeLookups` |
| `DeviceClaimService` | operações limitadas + `dynamodb:TransactWriteItems` somente nas tabelas necessárias para a conclusão atômica do claim |
| `membership reader` | `dynamodb:Query` somente na tabela `DeviceMemberships` e no GSI necessário |
| `manufacturing importer` | role administrativa separada (import de fabricação continua um fluxo manual/futuro) |
| App móvel | **nenhum** acesso direto ao DynamoDB — sempre via API (Fase 2/3) |

Nenhuma policy usará `dynamodb:*` nem `Resource: "*"`.

## Custo

Ver `docs/cost-controls.md` para a seção completa. Resumo: billing
on-demand mantém o custo ocioso próximo de zero, mas **armazenamento e
requisições do DynamoDB não são incondicionalmente gratuitos** —
cobrança por GB armazenado (além da tabela de PITR, aqui desativado) e
por unidade de leitura/escrita sob demanda além da cota gratuita mensal.

## Preservação da arquitetura BLE-first

Este modelo de dados implementa exatamente os campos conceituais já
registrados em `CONTEXT.md` ("Onboarding BLE-first") e
`docs/adr/0001-ble-first-onboarding.md` — nenhuma decisão de arquitetura de
onboarding foi revisada ou alterada por esta fase. BLE continua sendo o
fluxo primário; QR e digitação manual continuam fallbacks equivalentes que
carregam o mesmo `setup_code`; `claim_session` e o Fleet Provisioning
temporary claim continuam três conceitos distintos.
