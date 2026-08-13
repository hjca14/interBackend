# lambdas/

Placeholder for the Lambda function handlers used by `ApiStack` and
`IoTStack` (e.g. API endpoint handlers, IoT rule targets, Basic Ingest
processors).

## Estado atual

Nenhuma função Lambda foi implementada nesta fase (Fase 1A). Este diretório
existe apenas para fixar a estrutura do projeto e a convenção de onde o
código das funções deve viver quando as fases seguintes definirem:

- os contratos exatos da API HTTPS (`docs/deployment.md`, `CONTEXT.md`);
- o modelo de dados no DynamoDB (`infrastructure/stacks/data_stack.py`);
- as regras de IoT Basic Ingest (`infrastructure/stacks/iot_stack.py`).

Cada função deverá ter seu próprio subdiretório com o handler e seus testes
unitários correspondentes em `tests/unit/`.
