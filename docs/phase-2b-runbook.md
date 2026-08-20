# Runbook da Fase 2B (revisão futura; não executar neste PR)

## Estado e limites

A Fase 2A está concluída e a Fase 2B já foi implantada em DEV. A atualização da DataStack adicionou
somente outputs/exports, sem replacement das tabelas. A ApiStack foi implantada com Cognito, API
HTTP, três Lambdas, JWT Authorizer e KMS. O primeiro usuário Cognito foi criado e confirmado, sem
documentar e-mail, `sub` ou qualquer outro identificador real.

O primeiro Device/OWNER ainda não foi registrado. A primeira tentativa real foi recusada pelo IAM
com `AccessDeniedException` em `TransactWriteItems` e não escreveu nenhum item; nenhum identificador
real é registrado neste runbook. O bloqueio permanece seguro até que a correção da policy da role
operacional declarada neste PR seja revisada e implantada. A Fase 2C ainda não começou;
comandos/publicação MQTT permanecem na Fase 2D, e social login, MFA, SMS, Identity Pool e Hosted UI
continuam adiados. Este PR corretivo não executa chamadas AWS nem deploy.

## Decisões

O User Pool aceita somente e-mail verificado e senha (mínimo 8, maiúscula, minúscula e dígito;
símbolo não é exigido para equilibrar usabilidade), usa SRP, impede enumeração, não possui secret,
MFA/SMS/social nem domínio. Access e ID tokens duram 15 minutos, reduzindo a janela de token
roubado; refresh dura 7 dias, limitando sessões abandonadas. User Pool usa `RETAIN` e deletion
protection. O e-mail de verificação padrão tem assunto e corpo profissionais bilíngues
(português/inglês), identifica o InterBridge, inclui o código e orienta ignorar a mensagem caso a
conta não tenha sido solicitada. O remetente gerenciado padrão do Cognito permanece nesta fase.
Como melhoria futura, deve-se configurar Amazon SES com domínio próprio e autenticação SPF, DKIM e
DMARC antes de adotar um remetente personalizado; este PR não cria recursos SES.

As três Lambdas ARM64 têm permissões DynamoDB somente de leitura e específicas por tabela/índice.
Não há VPC, concorrência reservada ou IoT Publish. Logs duram uma semana. O cursor contém somente a
chave de paginação cifrada/autenticada pelo AWS KMS; `sub`/`limit` ficam no Encryption Context e não
no token. Base64 simples e HMAC sem confidencialidade foram rejeitados. A chave exclusiva de DEV
protege somente cursores efêmeros, custa aproximadamente US$ 1/mês armazenada, além das chamadas,
e não usa rotação automática (rotações adicionariam custo mensal). Sua substituição deliberada
invalida apenas cursores já emitidos, sem afetar usuários, devices ou telemetria. A estratégia de
rotação para produção fica para revisão futura; DEV mantém remoção em sete dias. BatchGet tenta no
máximo três vezes com exponential backoff e jitter.

Base64/JSON/estrutura inválidos e `InvalidCiphertextException` por adulteração retornam 400. Falhas
operacionais do KMS nunca são atribuídas ao cliente: indisponibilidade/timeout retorna 503 e demais
falhas internas retornam 500, sempre sem mensagem bruta do SDK em resposta ou log.

O authorizer continua nativo, mas audience não garante o tipo de token: cada Lambda exige também
`token_use=access` e `client_id` igual ao app client. ID token e claims divergentes recebem 401.

A ferramenta grava Device com `owner_user_id` e membership OWNER/ACTIVE na mesma
`TransactWriteItems`, usando ausência condicional em ambos. Esse marcador torna a unicidade do
OWNER atômica sem mudar chaves; retry só é aceito após leitura forte e igualdade integral. Como o
registry DEV está vazio, não há migração imediata; claim/transferência da Fase 3 deverá preservar e
alterar o marcador na própria transação.

Segundo a [documentação de atributos do Cognito](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-attributes.html),
o `sub` é um identificador opaco no formato próprio do serviço, não um UUID RFC. A ferramenta e as
Lambdas preservam o valor exatamente e aplicam apenas limites defensivos (não vazio, até 128
caracteres e sem controles). Na operação administrativa, a identidade continua comprovada por
`list_users` com filtro exato e pela comparação exata do atributo `sub` devolvido pelo Cognito.

## Sequência sujeita a autorização separada

1. Revisar templates sintetizados; depois autorizar `cdk diff` de Data e Api.
2. Confirmar que Data/IoT/Ingestion não têm replacement; autorizar deploy de Data e depois Api.
3. Obter outputs não secretos `ApiUrl`, `UserPoolId`, `UserPoolClientId` e `JwtIssuer`.
4. Criar o primeiro usuário por fluxo Cognito aprovado, digitando a senha somente em prompt seguro
   (nunca argumento, shell history ou log), confirmar o código de e-mail e consultar o atributo
   `sub` via operação administrativa somente leitura com credencial temporária.
5. Implantar a mudança **aditiva** da ApiStack somente após revisar o change set: ela acrescenta a
   role `interbridge-dev-device-registrar-role`, sua policy e um output de ARN não sensível. Não
   aceitar replacement do User Pool, tabelas ou qualquer recurso existente. A trust policy aceita
   somente principals da própria conta com MFA; isso não concede acesso por si só. Somente um
   principal que também tenha permissão de identidade explícita `sts:AssumeRole` para esse ARN pode
   utilizá-la. Não reutilizar roles de bootstrap ou deploy do CDK.
6. No CloudShell, desabilitar tracing e assumir a role sem exibir nem gravar as credenciais. Substituir
   apenas os placeholders; não colocar valores reais neste documento. O ARN do dispositivo MFA pode
   ser obtido pelo procedimento de inventário aprovado (não por tentativa):

   ```bash
   set +x
   export AWS_PAGER=""
   ROLE_ARN="arn:aws:iam::<ACCOUNT_ID>:role/interbridge-dev-device-registrar-role"
   MFA_ARN="arn:aws:iam::<ACCOUNT_ID>:mfa/<MFA_DEVICE_NAME>"
   read -r -s -p "MFA code: " MFA_CODE; printf '\n'
   export CREDS_JSON="$(aws sts assume-role \
     --role-arn "$ROLE_ARN" \
     --role-session-name interbridge-dev-device-registration \
     --serial-number "$MFA_ARN" \
     --token-code "$MFA_CODE" \
     --duration-seconds 900 \
     --output json)" || { unset MFA_CODE CREDS_JSON; return 1 2>/dev/null || exit 1; }
   unset MFA_CODE
   export AWS_ACCESS_KEY_ID="$(python -c 'import json,os; print(json.loads(os.environ["CREDS_JSON"])["Credentials"]["AccessKeyId"])')"
   export AWS_SECRET_ACCESS_KEY="$(python -c 'import json,os; print(json.loads(os.environ["CREDS_JSON"])["Credentials"]["SecretAccessKey"])')"
   export AWS_SESSION_TOKEN="$(python -c 'import json,os; print(json.loads(os.environ["CREDS_JSON"])["Credentials"]["SessionToken"])')"
   unset CREDS_JSON
   ```

   Não usar `set -x`, `env`, `export -p`, `aws configure`, `tee` ou redirecionamento para arquivo
   enquanto essas variáveis existirem. O comando não funciona até o principal de origem receber a
   autorização explícita citada acima.
7. A ferramenta faz leituras STS, CloudFormation, Cognito e IoT para resolver somente os recursos DEV
   esperados, localizar o usuário por `sub` exato e conferir Thing Type, Group, certificado ativo e
   policy, sem acessar chave privada. Ler os valores operacionais em variáveis (assim o histórico
   contém apenas os nomes das variáveis), executar obrigatoriamente o dry-run e revisar o resultado:

   ```bash
   read -r -p "Cognito sub: " COGNITO_SUB
   read -r -p "Device ID: " DEVICE_ID
   read -r -p "Hardware version: " HARDWARE_VERSION
   read -r -p "Manufacturing batch: " MANUFACTURING_BATCH
   python -m tools.register_dev_device \
     --environment dev --region sa-east-1 \
     --sub "$COGNITO_SUB" --device-id "$DEVICE_ID" \
     --hardware-version "$HARDWARE_VERSION" \
     --manufacturing-batch "$MANUFACTURING_BATCH" --dry-run
   ```

   Somente depois do dry-run bem-sucedido e da revisão por outra pessoa, executar a escrita atômica
   com a confirmação exata, novamente sem materializar credenciais ou identificadores em arquivos:

   ```bash
   CONFIRMATION="REGISTER DEV ${DEVICE_ID} OWNER ${COGNITO_SUB}"
   python -m tools.register_dev_device \
     --environment dev --region sa-east-1 \
     --sub "$COGNITO_SUB" --device-id "$DEVICE_ID" \
     --hardware-version "$HARDWARE_VERSION" \
     --manufacturing-batch "$MANUFACTURING_BATCH" --confirm "$CONFIRMATION"
   unset CONFIRMATION COGNITO_SUB DEVICE_ID HARDWARE_VERSION MANUFACTURING_BATCH
   unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN ROLE_ARN MFA_ARN
   ```

   A ferramenta recusa root, IAM user direto, federated-user direto, credencial permanente, qualquer
   caller que não seja `assumed-role` e qualquer assumed role cujo nome não seja exatamente
   `interbridge-dev-device-registrar-role`.
8. Obter access token sem registrá-lo e validar os três GETs com header bearer; testar também JWT
   ausente, ID inválido, lista vazia e ausência de health. Não testar comandos.

Rollback da API remove recursos efêmeros, mas não deve apagar tabelas nem User Pool, protegidos por
RETAIN/deletion protection. Não se deve contornar essa proteção durante rollback. CORS não está
habilitado porque Flutter mobile não precisa dele; eventual cliente web exigirá allowlist revisada.
