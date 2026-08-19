# Runbook da Fase 2B (revisão futura; não executar neste PR)

## Estado e limites

A Fase 2A está concluída. A Fase 2B está implementada e sintetizável localmente, mas não foi
implantada. Portanto User Pool, app client, HTTP API, usuários e registro do ESP32 DEV ainda não
existem na AWS. O app permanece na Fase 2C e comandos/publicação MQTT na Fase 2D; social login,
MFA, SMS, Identity Pool e Hosted UI continuam adiados.

## Decisões

O User Pool aceita somente e-mail verificado e senha (mínimo 10, maiúscula, minúscula e dígito;
símbolo não é exigido para equilibrar usabilidade), usa SRP, impede enumeração, não possui secret,
MFA/SMS/social nem domínio. Access e ID tokens duram 15 minutos, reduzindo a janela de token
roubado; refresh dura 7 dias, limitando sessões abandonadas. User Pool usa `RETAIN` e deletion
protection.

As três Lambdas ARM64 têm permissões DynamoDB somente de leitura e específicas por tabela/índice.
Não há VPC, concorrência reservada ou IoT Publish. Logs duram uma semana. O cursor contém a chave
de paginação e seu vínculo a `sub`/`limit`, autenticados por HMAC-SHA256. Base64 simples foi
rejeitado; KMS/tabela própria seriam mais complexos. Um segredo gerado pelo Secrets Manager é a
menor alternativa gerenciada sem plaintext no template, ao custo mensal e por chamada do serviço.

A ferramenta grava Device com `owner_user_id` e membership OWNER/ACTIVE na mesma
`TransactWriteItems`, usando ausência condicional em ambos. Esse marcador torna a unicidade do
OWNER atômica sem mudar chaves; retry só é aceito após leitura forte e igualdade integral. Como o
registry DEV está vazio, não há migração imediata; claim/transferência da Fase 3 deverá preservar e
alterar o marcador na própria transação.

## Sequência sujeita a autorização separada

1. Revisar templates sintetizados; depois autorizar `cdk diff` de Data e Api.
2. Confirmar que Data/IoT/Ingestion não têm replacement; autorizar deploy de Data e depois Api.
3. Obter outputs não secretos `ApiUrl`, `UserPoolId`, `UserPoolClientId` e `JwtIssuer`.
4. Criar o primeiro usuário por fluxo Cognito aprovado, digitando a senha somente em prompt seguro
   (nunca argumento, shell history ou log), confirmar o código de e-mail e consultar o atributo
   `sub` via operação administrativa somente leitura com credencial temporária.
5. Executar primeiro `python -m tools.register_dev_device --environment dev --region sa-east-1
   --sub <COGNITO_SUB> --device-id <DEVICE_ID> --hardware-version <VERSION>
   --manufacturing-batch <BATCH> --devices-table <DEVICES_TABLE> --memberships-table
   <MEMBERSHIPS_TABLE> --user-pool-id <POOL_ID> --dry-run`; somente após revisão repetir sem
   `--dry-run` e com a frase exata solicitada.
6. Obter access token sem registrá-lo e validar os três GETs com header bearer; testar também JWT
   ausente, ID inválido, lista vazia e ausência de health. Não testar comandos.

Rollback da API remove recursos efêmeros, mas não deve apagar tabelas nem User Pool, protegidos por
RETAIN/deletion protection. Não se deve contornar essa proteção durante rollback. CORS não está
habilitado porque Flutter mobile não precisa dele; eventual cliente web exigirá allowlist revisada.

