# Runbook de deploy da Fase 1E (comandos preparados; não executados neste PR)

> Estado: implementação local, sem chamadas AWS, diff real, deploy ou publicação MQTT. Execute
> cada bloco AWS somente após autorização explícita. Substitua apenas placeholders/variáveis.

## 1. Preparação e validação local

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-tools.txt
ruff format --check .
ruff check .
mypy infrastructure domain lambdas mqtt_smoke tools
pytest --cov --cov-report=term-missing
python scripts/check_secrets.py
git diff --check
AWS_REGION=sa-east-1 env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN npx cdk synth
```

## 2. Diff — somente após autorização e credenciais DEV confirmadas

```bash
export AWS_PROFILE='<DEV_PROFILE>' AWS_REGION='sa-east-1'
npx cdk diff InterBridge-Dev-DataStack
npx cdk diff InterBridge-Dev-IngestionStack
npx cdk diff InterBridge-Dev-ObservabilityStack
```

Esperado no DataStack: exatamente uma nova tabela DynamoDB `interbridge-dev-telemetry`, PK/SK,
TTL, PAY_PER_REQUEST, AWS-owned encryption, PITR off, deletion protection e RETAIN; nenhuma mudança
nas quatro tabelas existentes. Esperado no IngestionStack: duas TopicRules, uma Lambda/role/policy,
um log group, uma fila SQS, role/policy da error action, Lambda permission e dois outputs não
sensíveis. Esperado no ObservabilityStack: três alarmes. Interrompa se aparecer policy IoT do
dispositivo, Thing/certificado, registry table replacement, wildcard IAM, VPC/NAT ou KMS key.

## 3. Ordem e comandos de deploy separados

Data primeiro (cria export da tabela), Ingestion depois (consome o export), Observability por último
(consome referências de Lambda/fila). Aguarde aprovação independente antes de cada comando:

```bash
npx cdk deploy InterBridge-Dev-DataStack --require-approval broadening
npx cdk deploy InterBridge-Dev-IngestionStack --require-approval broadening
npx cdk deploy InterBridge-Dev-ObservabilityStack --require-approval broadening
```

## 4. Verificação somente leitura

```bash
export TABLE_NAME='interbridge-dev-telemetry'
export DEVICE_ID='<PROVISIONED_DEV_DEVICE_ID>'
export QUEUE_URL='<QUARANTINE_QUEUE_URL_FROM_STACK_OUTPUT>'
aws cloudformation describe-stacks --stack-name InterBridge-Dev-DataStack
aws cloudformation describe-stacks --stack-name InterBridge-Dev-IngestionStack
aws cloudformation describe-stacks --stack-name InterBridge-Dev-ObservabilityStack
aws iot get-topic-rule --rule-name interbridge_dev_ingest_rule
aws iot get-topic-rule --rule-name interbridge_dev_response_rule
aws dynamodb describe-table --table-name "$TABLE_NAME"
aws dynamodb get-item --table-name "$TABLE_NAME" --key '{"device_id":{"S":"'"$DEVICE_ID"'"},"record_key":{"S":"STATE#CURRENT"}}'
aws dynamodb query --table-name "$TABLE_NAME" --key-condition-expression 'device_id = :d AND begins_with(record_key, :p)' --expression-attribute-values '{":d":{"S":"'"$DEVICE_ID"'"},":p":{"S":"EVENT#"}}'
aws dynamodb query --table-name "$TABLE_NAME" --key-condition-expression 'device_id = :d AND begins_with(record_key, :p)' --expression-attribute-values '{":d":{"S":"'"$DEVICE_ID"'"},":p":{"S":"RESPONSE#"}}'
aws dynamodb query --table-name "$TABLE_NAME" --key-condition-expression 'device_id = :d AND begins_with(record_key, :p)' --expression-attribute-values '{":d":{"S":"'"$DEVICE_ID"'"},":p":{"S":"METRIC#"}}'
aws cloudwatch describe-alarms --alarm-name-prefix interbridge-dev-monitoring-
aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

## 5. ESP32 já provisionado

Não reprovisione nem altere Thing/certificado. Depois do deploy autorizado, ligue o mesmo ESP32 e
observe somente o comportamento já contratado: o health QoS 0 atualiza `STATE#CURRENT` e a métrica.
Um comando seguro já previsto pelo protocolo, se separado e explicitamente autorizado, produz a
resposta QoS 1. Este runbook não autoriza publicar MQTT.

## 6. Alarmes, quarentena e rollback

Qualquer mensagem visível deixa o alarme de quarentena em ALARM; inspecione atributos primeiro. O
corpo sanitizado contém apenas motivo canônico, categoria, device_id validado e horário — nunca o
payload. Erros/throttles da Lambda também alarmam.

Rollback seguro: pare/remova primeiro ObservabilityStack; desabilite as duas rules antes de remover
IngestionStack; preserve a fila para investigação conforme decisão operacional. Faça rollback do
DataStack por último. A tabela possui RETAIN + deletion protection: `cdk destroy` não a apaga. Não
desabilite proteção nem apague dados sem uma decisão separada. Exports impedem remoção do DataStack
enquanto consumidores existirem, portanto respeite a ordem inversa.
