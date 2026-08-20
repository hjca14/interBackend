# Runbook da Fase 2B (revisão futura; não executar neste PR)

## Estado e limites

A Fase 2A está concluída. A Fase 2B está implementada e sintetizável localmente, mas não foi
implantada. Portanto User Pool, app client, HTTP API, usuários e registro do ESP32 DEV ainda não
existem na AWS. O app permanece na Fase 2C e comandos/publicação MQTT na Fase 2D; social login,
MFA, SMS, Identity Pool e Hosted UI continuam adiados.

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
5. A ferramenta faz leituras STS, CloudFormation, Cognito e IoT para resolver outputs das stacks DEV
   exatas, localizar o usuário por filtro exato de `sub` e conferir Thing Type, Group, certificado
   ativo e policy, sem acessar chave privada. Executar primeiro
   `python -m tools.register_dev_device --environment dev --region sa-east-1
   --sub <COGNITO_SUB> --device-id <DEVICE_ID> --hardware-version <VERSION>
   --manufacturing-batch <BATCH> --dry-run`; somente após revisão repetir sem
   `--dry-run` e com a frase exata solicitada.
6. Obter access token sem registrá-lo e validar os três GETs com header bearer; testar também JWT
   ausente, ID inválido, lista vazia e ausência de health. Não testar comandos.

Rollback da API remove recursos efêmeros, mas não deve apagar tabelas nem User Pool, protegidos por
RETAIN/deletion protection. Não se deve contornar essa proteção durante rollback. CORS não está
habilitado porque Flutter mobile não precisa dele; eventual cliente web exigirá allowlist revisada.
