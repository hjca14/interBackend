# Phase 1D.1 — DEV MQTT/mTLS smoke test

This runbook prepares a **computer-side device simulator**. It is not a backend command
publisher, never performs a physical action, and is DEV-only. Phase 1D.1 is locally prepared;
it has not been deployed or cloud-validated.

## Safety boundary and current limitation

The two Basic Ingest rules are reserved but do not exist until Phase 1E. A test can validate
the mTLS connection, command subscription, delivery, and MQTT publish/PUBACK behavior now.
Health, event, and response publications will **not** be persisted in or visible through
DynamoDB. Do not create a temporary normal-broker diagnostic topic and do not change the CDK
stacks to work around this boundary.

Never use root access keys. Prefer a short-lived, least-privilege operator session for the
manual console steps. Never commit certificates or private keys. Every test device gets its
own unique certificate. The manual certificate described here is DEV-only. Production remains
Fleet Provisioning with CSR and on-device permanent-key generation.

## One-time local setup

Use a controlled PC (preferred) or CloudShell with Python 3.11/3.12. A PC is required when the
private key must remain under local operator control; CloudShell storage should not be treated
as a permanent credential vault.

```text
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-mqtt-smoke.txt
```

Store the device certificate, its private key, and Amazon Root CA outside this repository with
owner-only permissions. The CLI accepts paths only—it never accepts PEM contents. The repository
ignores common certificate/key extensions and `certificates/`, but ignore rules are not a vault.

## Controlled manual provisioning (do not run as part of repository validation)

Perform these steps later in the AWS console or with individually reviewed AWS CLI commands:

1. Choose a non-production identifier matching `ib-` followed by exactly 32 lowercase hex
   characters. Never paste a real identifier into documentation.
2. In `dev`, region `sa-east-1`, create one Thing whose name is exactly that identifier and whose
   Thing Type is the existing `interbridge-dev-device`.
3. Add it to the existing `interbridge-dev-devices` Thing Group.
4. Create one new certificate, active, for this DEV test device. Securely save the private key at
   creation time; AWS cannot provide it again.
5. Attach that certificate to the Thing.
6. Attach the existing shared policy `interbridge-dev-device-policy` to the certificate.
7. Obtain the account-specific **IoT data-ATS endpoint**. Record only its hostname locally; do not
   include `https://`, a port, or a path and do not commit it.
8. Download Amazon Root CA 1 from Amazon's official trust repository over HTTPS and verify that
   the downloaded file is the intended CA file.
9. Place all three credential files outside the repository, use restrictive file permissions,
   and ensure the certificate, private key, and CA paths are three different regular files.

These operations create/mutate AWS resources and are intentionally **not executed** by normal
local validation, tests, synthesis, or this preparation phase.

## Run the simulator

Use placeholders locally; do not copy credentials into shell arguments or this document:

```text
python -m mqtt_smoke \
  --endpoint <IOT_DATA_ATS_HOSTNAME> \
  --device-id <NON_PRODUCTION_DEVICE_ID> \
  --certificate /secure/outside-repo/device-certificate.crt \
  --private-key /secure/outside-repo/device-private.key \
  --root-ca /secure/outside-repo/AmazonRootCA1.pem
```

Port 8883 is the default. Equivalent environment variables are
`INTERBRIDGE_IOT_ENDPOINT`, `INTERBRIDGE_DEVICE_ID`, `INTERBRIDGE_CERTIFICATE_PATH`,
`INTERBRIDGE_PRIVATE_KEY_PATH`, `INTERBRIDGE_ROOT_CA_PATH`, and optionally
`INTERBRIDGE_IOT_PORT`. Put path values—not PEM content—in a local `mqtt-smoke.env` only;
that filename is ignored. The simulator requires server certificate verification, explicitly
disables insecure TLS, uses MQTT 3.1.1, and uses `ClientId == device_id`.

Expected safe activity:

| Operation | Topic | QoS | Retained |
| --- | --- | ---: | --- |
| Subscribe | `interbridge/{device_id}/commands` | 1 | n/a |
| Health publish | `$aws/rules/interbridge_dev_ingest_rule/interbridge/{device_id}/health` | 0 | no |
| Safe `ERROR` event | `$aws/rules/interbridge_dev_ingest_rule/interbridge/{device_id}/events` | 1 | no |
| Rejected response | `$aws/rules/interbridge_dev_response_rule/interbridge/{device_id}/responses` | 1 | no |

The console reports connection and subscription acknowledgements plus publish acknowledgements.
QoS 0 health has no broker PUBACK by MQTT design; the client callback may still report completion.

## Send one safe command from the AWS console

In the AWS IoT MQTT test client, publish a protocol-v1 command to the exact commands topic for the
test device, at QoS 1. Use a fresh 32-character lowercase hexadecimal `command_id`, and a command
already defined by protocol v1 such as `OPEN_DOOR`. Do not use retained delivery.

The simulator validates JSON, the protocol version, command ID, command name, and the 8 KiB maximum.
It preserves the validated `command_id` and `command`, performs **no action**, and publishes only a
protocol-v1 `REJECTED` response with `COMMAND_NOT_ALLOWED`. It never reports `COMPLETED`. Malformed,
oversized, and unknown commands produce only a safe summary; arbitrary fields are not logged.
Observe the incoming-command summary and the response publish acknowledgement locally. Because the
Phase 1E response rule is absent, do not expect a response message in a normal console subscription
or any DynamoDB record.

## Finish explicitly

Stop the simulator. Then choose and record one of these DEV-only outcomes:

- **Retain for the next controlled test:** keep the Thing and certificate, store the private key
  securely, and record an owner/review date outside Git; or
- **Clean up:** after reviewing dependencies, detach the policy and Thing principal, deactivate and
  delete the certificate, remove the Thing from the group, and delete the Thing.

Cleanup is an AWS mutation and must be separately authorized and performed manually. Never reuse
this manual DEV credential for production or share one certificate between test devices.
