# ADR 0003 — Autenticação Cognito e autorização por membership

- **Status:** aceito para implementação futura
- **Data:** 2026-08-19
- **Escopo:** Fase 2A (decisão documental; nenhum recurso existe por causa deste ADR)

## Contexto

O `interapp` precisa acessar uma API HTTPS sem receber credenciais AWS e sem usar e-mail como
identidade de domínio. A camada pública ainda não existe. O Device Registry e
`DeviceMemberships` já existem em DEV, enquanto o `ApiStack` continua vazio. O ambiente inicial é
somente DEV em `sa-east-1`.

## Decisão

### Identidade e autenticação

Na Fase 2B será usado **Amazon Cognito User Pool**, com login apenas por e-mail e senha. E-mail é
obrigatório, confirmado por código, e a recuperação de senha é habilitada. Login por telefone,
SMS, provedores sociais e MFA ficam adiados. MFA futuro deverá admitir mecanismos que não tornem
SMS obrigatório.

O claim imutável `sub` do JWT validado é a identidade canônica. E-mail é atributo mutável de login
e contato: nunca é chave de propriedade/autorização. Nenhuma rota aceita `user_id`, `owner_id` ou
equivalente do cliente. Uma futura federação social terá de vincular a identidade ao mesmo usuário
interno de forma explícita e segura, evitando criar dois `sub` lógicos para a mesma pessoa; os
contratos HTTP e as chaves internas continuam baseados na identidade canônica, não no provedor.

Não haverá Cognito Identity Pool. O app não recebe access key, secret key ou session token AWS:
ele apresenta um bearer JWT somente à API HTTPS.

### API Gateway e validação do JWT

A escolha para a Fase 2B é **API Gateway HTTP API com JWT Authorizer**. Ela satisfaz as rotas HTTP,
validação nativa do token e integração Lambda com menor complexidade e custo qualitativo que REST
API. REST API foi considerada, mas seus recursos adicionais (por exemplo, usage plans/API keys,
transformações avançadas e recursos legados) não são requisitos atuais; API key também não
substituiria autenticação do usuário. A decisão deve ser reavaliada se surgir requisito concreto
que HTTP API não suporte.

O authorizer será configurado com o issuer HTTPS exato do User Pool DEV e a audience/client ID
exata do app client. Serão aceitos apenas JWTs assinados por algoritmo explicitamente esperado e
publicado pelo issuer; assinatura e `kid` serão verificados contra JWKS confiável, com rotação e
cache limitados. Tokens expirados, ainda não válidos, com issuer ou audience/client ID incorretos,
assinatura inválida ou algoritmo inesperado são rejeitados como `401 UNAUTHENTICATED`. O backend
usa somente claims do contexto já validado e deve distinguir o tipo de token apropriado à API
(access token; a configuração final e os testes de claims ficam como pendência da 2B). Relógios e
pequena tolerância de clock skew deverão ser explícitos e testados, nunca usados para prolongar
tokens arbitrariamente.

Logout encerra a sessão local e a Fase 2B deve avaliar revogação/Global Sign-out. Revogar refresh
tokens impede renovação, mas um access token já emitido pode continuar válido até expirar; por
isso sua vida deve ser curta e a autorização de membership deve ocorrer em cada requisição, sem
ser congelada no token.

### Autorização

Após autenticar, cada acesso a dispositivo consulta `DeviceMemberships`. Apenas status `ACTIVE`
autoriza. `REMOVED` ou qualquer valor desconhecido falha fechado. Os enums reais são `OWNER`,
`ADMIN` e `MEMBER`; somente `OWNER` tem suporte inicial garantido. A matriz detalhada está em
`docs/phase-2-architecture.md`. Membership do produto é distinta da autorização do certificado
X.509/IoT Policy do dispositivo.

Para rotas de recurso, inexistência e ausência de membership retornam a mesma resposta
`404 RESOURCE_NOT_FOUND`, com corpo e timing operacionalmente tão uniformes quanto praticável.
Isso impede que um usuário autenticado enumere `device_id`. `COMMAND_NOT_FOUND` só é retornado
depois de validar membership ativa no dispositivo da URL.

### Dados sensíveis e observabilidade

Logs nunca contêm JWT/access/refresh/ID token, senha, código de confirmação/recuperação, headers de
autorização, payload bruto sensível, setup code, chaves ou certificados. Erros públicos não expõem
stack trace, IAM, nomes de tabela ou mensagens internas da AWS. Auditoria usa identificadores
sanitizados/correlacionáveis e códigos de resultado, com retenção explícita.

## Alternativas consideradas

- **Autenticação própria:** rejeitada por ampliar o risco e a manutenção de senha, verificação e
  recuperação.
- **Provedor externo/SaaS ou federação social imediata:** adiado; adiciona dependência e fluxos de
  vinculação sem necessidade para DEV.
- **IAM/Cognito Identity Pool no app:** rejeitado; exporia credenciais AWS e aumentaria a superfície
  de autorização.
- **API Gateway REST API:** não há requisito atual que justifique custo e complexidade adicionais.
- **Telefone/SMS e MFA imediato:** adiados; e-mail/senha confirmado é o menor fluxo inicial e não
  acopla o produto a SMS.

## Consequências

A Fase 2B terá de provisionar e testar User Pool, app client sem segredo no cliente público, HTTP
API, authorizer, Lambdas e IAM em PR separado. Indisponibilidade do Cognito pode impedir login ou
renovação; tokens válidos poderão continuar chegando à API. A API deve falhar fechada quando não
puder validar identidade/membership e usar erros sanitizados (`401`, `404` ou `503`, conforme o
ponto da falha). Federação, vinculação de contas, MFA, política final de duração/revogação e
controles adaptativos continuam decisões explícitas futuras.
