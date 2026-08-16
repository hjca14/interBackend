# Fase 1D — dispositivo MQTT/mTLS DEV controlado

## Estado e limite

**Fase 1D concluída no escopo definido: primeiro dispositivo DEV controlado validado por MQTT/mTLS
no simulador e no ESP32-C3 real.** O teste físico reutilizou o mesmo Thing DEV e certificado X.509
individual do simulador. Em uma ESP32-C3 Super Mini genérica de bancada (ESP32-C3, 4 MB de flash,
USB-C/USB nativa), o firmware PlatformIO compatível com `esp32-c3-devkitm-1` foi compilado, enviado
por USB e inicializado. Foram validados Wi-Fi 2,4 GHz, endpoint Data ATS, porta 8883, mTLS com Amazon
Root CA 1, `ClientId` igual/derivado do `device_id`, policy vinculada, assinatura QoS 1, health QoS 0,
recepção de `OPEN_DOOR` publicado pela AWS CLI com JSON em Base64 no PowerShell e resposta segura,
sem ação física, confirmada por `response publish: ok`.

Após desligamento completo, o novo boot reconectou ao Wi-Fi/AWS, recebeu outro comando e publicou
outra resposta segura. Falhas DNS transitórias foram superadas pelas retentativas existentes após
validação externa dos registros A e AAAA; nenhum endpoint ou IP é registrado aqui. **Não foi testada
a queda/retorno do ponto de acesso com a placa ainda ligada** — essa pendência não invalida o boot
frio já validado. A placa de bancada não define o módulo final da PCB comercial.

A CLI permanece exclusiva para um dispositivo controlado em `dev`/`sa-east-1`; não altera CDK nem
escreve no DynamoDB. Permanecem pendentes Fleet Provisioning, geração de chave no dispositivo, CSR,
onboarding BLE/app/NVS, hardening e produção, além do cleanup ou decisão formal de retenção do Thing
DEV. Basic Ingest/persistência pertencem à Fase 1E e não foram implementados neste trabalho.

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
