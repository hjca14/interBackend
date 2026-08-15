# Fase 1D — dispositivo MQTT/mTLS DEV controlado

## Estado e limite

**Validada uma vez em DEV, via simulador de computador.** Um primeiro uso real desta CLI
(`provision`, depois `verify`) criou com sucesso um único Thing DEV descartável e seu
certificado X.509 exclusivo, obteve o endpoint `iot:Data-ATS` e gravou as credenciais fora do
repositório; o simulador do smoke test (`docs/mqtt-smoke-test.md`) então conectou por MQTT/mTLS,
confirmou a assinatura QoS 1 e publicou/recebeu mensagens com sucesso. Nenhum identificador real
(conta, ARN, endpoint, `device_id`, `certificate_id`, caminhos locais) é registrado neste
repositório; apenas o fato da validação. **O firmware real do ESP32-C3 ainda não foi testado** —
apenas o simulador foi. A CLI ainda é exclusiva para um dispositivo descartável em
`dev`/`sa-east-1`; não altera CDK nem escreve nas tabelas DynamoDB. Fleet Provisioning de produção
continua pendente; produção continuará usando Fleet Provisioning by Trusted User, CSR e chave
permanente gerada dentro do ESP. A decisão de reter ou limpar (`cleanup`) o dispositivo DEV usado
neste teste também continua pendente.

## Pré-requisitos e autenticação

Use Python 3.12 em um computador controlado, nunca credenciais do root user da conta:

```text
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-tools.txt
aws sso login --profile <perfil-operador-de-menor-privilégio>
export AWS_PROFILE=<perfil-operador-de-menor-privilégio>
```

O operador precisa de STS `GetCallerIdentity` e das ações IoT estritamente necessárias às
operações listadas abaixo. A ferramenta não contém credenciais. Escolha localmente um ID novo no
formato `ib-` + 32 hexadecimais minúsculos; não o cole em documentação.

## Cofre local fora do Git

Escolha explicitamente um diretório externo ao checkout, idealmente em volume criptografado. A
CLI recusa qualquer caminho dentro do repositório, não sobrescreve diretório não vazio e cria o
diretório como `0700` e cada arquivo como `0600`, quando suportado. Ela produzirá certificado,
chave, endpoint e metadados. `AmazonRootCA1.pem` deve ser baixado manualmente do repositório de
confiança oficial da Amazon por HTTPS, validado e salvo no mesmo diretório como `0600`; a tarefa
não faz download de rede. Nunca mova esses arquivos para Git.

A proteção localiza a raiz real do checkout a partir do próprio módulo e valida `.git` mais os
marcadores do projeto; portanto funciona da mesma forma quando a CLI é iniciada na raiz ou em
qualquer subdiretório. Se essa raiz não puder ser determinada com segurança, a execução é recusada.

```text
device-certificate.pem.crt
private.pem.key
AmazonRootCA1.pem                 # passo manual
endpoint.txt
device-metadata.json
```

O JSON não contém chave, Wi-Fi, `setup_code`, `claim_session` ou credenciais AWS.

## Uso controlado

Cada mutação mostra conta, região e dispositivo e exige digitar a frase exibida. `--confirm`
existe para uma sessão não interativa deliberadamente revisada e exige a mesma frase exata.

```text
python -m tools.dev_iot_device provision --device-id ib-<32hex> \
  --region sa-east-1 --output-dir /caminho/externo --dry-run
python -m tools.dev_iot_device provision --device-id ib-<32hex> \
  --region sa-east-1 --output-dir /caminho/externo
python -m tools.dev_iot_device verify --device-id ib-<32hex> --region sa-east-1
python -m tools.dev_iot_device cleanup --device-id ib-<32hex> \
  --region sa-east-1 --dry-run
python -m tools.dev_iot_device cleanup --device-id ib-<32hex> --region sa-east-1
```

O provisionamento faz, nesta ordem: STS; confirmação; valida ausência do Thing/arquivos; cria o
Thing com type `interbridge-dev-device`; adiciona ao group `interbridge-dev-devices`; emite e ativa
certificado exclusivo; anexa certificado ao Thing; anexa `interbridge-dev-device-policy`; obtém
endpoint `iot:Data-ATS`; grava os arquivos localmente. Falha parcial é reportada e requer inspeção
e cleanup controlado — a CLI não tenta rollback agressivo nem reutiliza silenciosamente credencial.

`verify` é somente leitura e idempotente. Exige exatamente type, único group esperado, um único
certificado e uma única policy esperada. Cleanup faz as mesmas verificações antes de: desanexar a
policy; desanexar o certificado; desativá-lo; excluí-lo; remover o Thing do group; excluir o Thing.
Qualquer vínculo inesperado interrompe tudo para intervenção manual.

## Smoke test, firmware e simulador

Depois de provisionar e baixar manualmente a Root CA, preencha **somente no computador do
operador** o header/configuração local ignorado pelo Git do firmware: `device_id` como Thing Name e
Client ID, hostname de `endpoint.txt`, porta `8883`, caminhos/conteúdos locais do certificado,
chave e Root CA. Não cole Wi-Fi ou PEM em commits. Consulte o repositório do firmware para os nomes
exatos dos campos; esta ferramenta não altera esse repositório.

Para o simulador do backend, siga `docs/mqtt-smoke-test.md` e passe os cinco valores pelos paths
externos. O operador ainda deverá: autenticar; revisar o dry-run; provisionar; baixar/verificar a
Root CA; configurar firmware ou simulador; executar o MQTT/mTLS real; registrar apenas resultados
não sensíveis fora de logs; executar `verify`; e finalmente executar dry-run + cleanup. Basic
Ingest/persistência ainda não existe, portanto não insira registros de teste no DynamoDB.
