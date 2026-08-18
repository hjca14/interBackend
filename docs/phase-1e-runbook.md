# Runbook operacional da Fase 1E (implantada e validada em DEV)

> Estado: implementação, deploy e validação ponta a ponta concluídos em DEV/`sa-east-1` em
> 2026-08-18. Este documento preserva os comandos para referência; qualquer nova chamada AWS de
> escrita continua exigindo autorização explícita. Substitua apenas placeholders/variáveis.

## 1. Preparação e validação local

As Topic Rules acrescentam exclusivamente no backend `ibmeta_device_id`, `ibmeta_category` e
`ibmeta_received_at`; esses campos não pertencem ao protocolo publicado pelo firmware. Seus
predicados `isUndefined(...)` são intencionais: como o AWS IoT avalia `WHERE` antes de `SELECT`,
eles verificam o payload MQTT original e rejeitam colisões fornecidas pelo dispositivo.

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

## 2. Diff para mudanças futuras — somente após autorização e credenciais DEV confirmadas

```bash
export AWS_PROFILE='<DEV_PROFILE>' AWS_REGION='sa-east-1'
npx cdk diff InterBridge-Dev-DataStack
npx cdk diff InterBridge-Dev-IngestionStack
npx cdk diff InterBridge-Dev-ObservabilityStack
```

Esperado no DataStack: exatamente uma nova tabela DynamoDB `interbridge-dev-telemetry`, PK/SK,
TTL, PAY_PER_REQUEST, AWS-owned encryption, PITR off, deletion protection e RETAIN; nenhuma mudança
nas quatro tabelas existentes. Esperado no IngestionStack: duas TopicRules, uma Lambda/role/policy,
um log group, uma quarentena sanitizada, uma DLQ técnica, role/policy da error action, duas Lambda permissions restritas e três outputs não sensíveis. Esperado no ObservabilityStack: quatro alarmes. Interrompa se aparecer policy IoT do
dispositivo, Thing/certificado, registry table replacement, wildcard IAM, VPC/NAT ou KMS key.

## 3. Ordem usada no deploy e comandos para mudanças futuras

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
export INVALID_QUEUE_URL='<INVALID_QUARANTINE_URL>'
export TECHNICAL_DLQ_URL='<TECHNICAL_DLQ_URL>'
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
aws sqs get-queue-attributes --queue-url "$INVALID_QUEUE_URL" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
aws sqs get-queue-attributes --queue-url "$TECHNICAL_DLQ_URL" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

## 5. ESP32 já provisionado

Não reprovisione nem altere Thing/certificado. Depois do deploy autorizado, ligue o mesmo ESP32 e
observe somente o comportamento já contratado: o health QoS 0 atualiza `STATE#CURRENT` e a métrica.
Um comando seguro já previsto pelo protocolo, se separado e explicitamente autorizado, produz a
resposta QoS 1. Este runbook não autoriza publicar MQTT.

## 6. Resultado real de 2026-08-18

Os deploys de `InterBridge-Dev-DataStack`, `InterBridge-Dev-IngestionStack` e
`InterBridge-Dev-ObservabilityStack` foram concluídos com sucesso em DEV/`sa-east-1`. A tabela
`interbridge-dev-telemetry` foi criada em modo on-demand, com TTL `expires_at` nos itens
temporários, preservando as quatro tabelas da Fase 1C. A ingestão criou a Lambda
`interbridge-dev-ingestion-telemetry-handler`, duas Topic Rules de Basic Ingest, quarentena
sanitizada e DLQ técnica. A ObservabilityStack criou os quatro alarmes previstos.

A reserved concurrency da Lambda não é configurada em DEV: o limite regional efetivo observado é
10, e uma reserva impediria manter o mínimo exigido de 10 execuções não reservadas. Os aliases
internos compatíveis com AWS IoT SQL são `ibmeta_device_id`, `ibmeta_category` e
`ibmeta_received_at`; aliases iniciados por underscore foram rejeitados. Preserve os guards
`isUndefined(...)`: o AWS IoT avalia `WHERE` sobre o payload original antes de aplicar `SELECT`.

No teste com ESP32-C3 Super Mini, MQTT/mTLS e assinatura de commands QoS 1 funcionaram; health QoS
0 e responses QoS 1 foram persistidos. Comandos do AWS IoT MQTT Test Client chegaram à placa. A
consulta observou `STATE#CURRENT`, `METRIC#...` e `RESPONSE#...`, com `health_count=4`,
`response_count=5` e `detailed_count=5`. As cinco respostas foram corretamente
`REJECTED`/`COMMAND_EXPIRED`: os `OPEN_DOOR` foram publicados após sua validade de 10 segundos. O
fluxo AWS IoT commands → ESP32 → responses → Basic Ingest → Lambda → DynamoDB foi comprovado; ações
físicas não foram executadas nem validadas, pois o smoke firmware as bloqueia intencionalmente.

## 7. Validações ainda pendentes

- payload inválido controlado chegando à quarentena;
- falha controlada chegando à DLQ técnica;
- transição real dos quatro alarmes para `ALARM`;
- perda e recuperação do access point com a placa energizada;
- autenticação/API pública;
- BLE/Fleet Provisioning;
- ações físicas do interfone.

## 8. Alarmes, quarentena e rollback

A quarentena de mensagens inválidas recebe exclusivamente o envelope sanitizado (reason_code, categoria, device_id validado e horário), nunca o payload. A DLQ técnica recebe falhas assíncronas da Lambda e o errorAction da rule e **pode conter o evento original**; trate-a como sensível. Cada fila possui alarme próprio, além de erros/throttles da Lambda.

Rollback seguro: pare/remova primeiro ObservabilityStack; desabilite as duas rules antes de remover
IngestionStack; preserve a fila para investigação conforme decisão operacional. Faça rollback do
DataStack por último. A tabela possui RETAIN + deletion protection: `cdk destroy` não a apaga. Não
desabilite proteção nem apague dados sem uma decisão separada. Exports impedem remoção do DataStack
enquanto consumidores existirem, portanto respeite a ordem inversa.
