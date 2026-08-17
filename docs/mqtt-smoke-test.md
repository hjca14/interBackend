# Phase 1D.1 — DEV MQTT/mTLS smoke test

This runbook prepares a **computer-side device simulator**. It is not a backend command
publisher, never performs a physical action, and is DEV-only.

**Status: validated in DEV by both the computer simulator and real ESP32-C3 hardware.** The
simulator first completed the safe end-to-end flow. The same controlled DEV Thing and individual
certificate were then used by the real firmware on a generic ESP32-C3 Super Mini bench board. The
board connected over Wi-Fi 2.4 GHz and MQTT/mTLS, subscribed at QoS 1, published initial health at
QoS 0, received `OPEN_DOOR` without performing a physical action, and published a safe response
confirmed by serial output `response publish: ok`. A complete power-off/new boot also reconnected
and completed a second command/response flow. Phase 1D is complete only in the scope stated in
`docs/phases.md`; this is not production onboarding or Fleet Provisioning.

## Real ESP32-C3 validation boundary

The bench hardware was a generic ESP32-C3 Super Mini (ESP32-C3, 4 MB flash, native USB over USB-C),
with firmware built by PlatformIO in an environment compatible with `esp32-c3-devkitm-1`. This does
not select or promise the final commercial PCB module. The firmware used the AWS IoT Data ATS
endpoint on port 8883, Amazon Root CA 1, its unique X.509 certificate, and the corresponding private
key stored only in the operator's local DEV smoke environment. `ClientId` was equal to or derived
from `device_id`, as required by the current protocol/policy.

Transient DNS failures were observed. External DNS checks confirmed A and AAAA records without
recording the real endpoint or any address here, and the existing firmware retries subsequently
connected. Cold boot and reconnection after fully powering the board off and on are validated. A
Wi-Fi access-point outage and recovery **while the ESP32 remains powered on has not been tested**
and remains explicitly pending.

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

## Controlled provisioning (do not run as part of repository validation)

The safer local operational CLI and its exact runbook are now documented in
`docs/phase-1d-dev-device.md`; use that flow instead of assembling ad-hoc AWS CLI commands. The
steps below remain the conceptual checklist, not permission to execute them.

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
local validation, tests, synthesis, or this preparation phase — they run only when an operator
deliberately invokes `tools/dev_iot_device.py` (`docs/phase-1d-dev-device.md`). This exact
sequence (steps 1-9) is what that tool already executed successfully once, end to end, for the
DEV smoke test documented at the top of this file.

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

## Send one safe command from Windows PowerShell (AWS CLI)

Publishing the same protocol-v1 command from `aws iot-data publish` on Windows PowerShell needs one
extra step that the console does not: **raw JSON passed as a command-line argument can have its
quotes stripped or altered by PowerShell before AWS CLI ever sees it.** During the real Phase 1D.1
smoke test, publishing raw `--payload '{"protocol_version":1,...}'` this way produced a malformed
payload once it reached the simulator — PowerShell's argument parsing had already damaged the
quoting. The simulator correctly rejected it, which was itself a useful confirmation that its
fail-closed JSON parsing behaves as intended even for locally mangled input, not just for a
genuinely hostile payload.

The fix is to never hand PowerShell a raw JSON string as a CLI argument at all: build the command
object in PowerShell, serialize it, encode the UTF-8 bytes as Base64 yourself, and let AWS CLI's
**default** blob handling take it from there — do **not** add `--cli-binary-format
raw-in-base64-out` here, since that flag changes the CLI to expect *raw* bytes and encode them
itself, which is the opposite of what an already-Base64 string needs.

```powershell
# 1. command_id: 32 lowercase hex chars, generated with Python's CSPRNG
#    (secrets.token_hex(16) -> 16 random bytes -> 32 hex characters).
$commandId = python -c "import secrets; print(secrets.token_hex(16))"

# 2. issued_at / expires_at: Unix epoch seconds, short validity window.
$issuedAt  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$expiresAt = $issuedAt + 60

# 3. Build the protocol-v1 command object and compact-serialize it.
#    Placeholders only -- ib-<32hex> is not a real device_id.
$deviceId = "ib-<32hex>"
$command = [ordered]@{
    protocol_version = 1
    command_id       = $commandId
    command          = "OPEN_DOOR"
    issued_at        = $issuedAt
    expires_at       = $expiresAt
} | ConvertTo-Json -Compress

# 4. UTF-8 -> Base64. This is what actually avoids the PowerShell
#    quote-mangling problem: the CLI argument is now plain Base64 text,
#    which PowerShell cannot corrupt by "helpfully" reinterpreting quotes.
$payloadBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($command))

# 5. Publish at QoS 1, not retained, to the device's own commands topic.
#    No --cli-binary-format flag: the default blob format already expects
#    a Base64 string, matching what we just produced.
aws iot-data publish `
    --topic "interbridge/$deviceId/commands" `
    --qos 1 `
    --payload $payloadBase64
```

This produces a valid, protocol-v1-shaped command that the simulator accepts, parses, and — because
Phase 1D.1 always runs with command execution disabled — rejects safely with a `REJECTED` /
`COMMAND_NOT_ALLOWED` response, exactly like the AWS-console flow above. No physical action is ever
taken by the simulator, on Windows or otherwise.

## Finish explicitly

Stop the simulator. Then choose and record one of these DEV-only outcomes:

- **Retain for the next controlled test:** keep the Thing and certificate, store the private key
  securely, and record an owner/review date outside Git; or
- **Clean up:** after reviewing dependencies, detach the policy and Thing principal, deactivate and
  delete the certificate, remove the Thing from the group, and delete the Thing.

Cleanup is an AWS mutation and must be separately authorized and performed manually. Never reuse
this manual DEV credential for production or share one certificate between test devices.

## Phase 1E persistence follow-up (not deployed yet)

After an explicitly authorized Phase 1E deployment, the already provisioned ESP32 may be powered
on unchanged: its protocol-v1 Basic Ingest topics and QoS remain exactly those above. Do not
re-provision, replace its certificate, publish a synthetic event, or clear it merely to test
persistence. Follow the read-only queries and alarm/queue checks in `docs/phase-1e-runbook.md`.
Until that deployment and real-device verification occur, Phase 1E is only locally implemented.
