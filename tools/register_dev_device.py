"""Controlled DEV registry operation. Importing or dry-running performs no AWS writes."""

from __future__ import annotations

import argparse
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, TypeGuard

from domain.devices.identifiers import validate_device_id

REGION = "sa-east-1"
DATA_STACK = "InterBridge-Dev-DataStack"
API_STACK = "InterBridge-Dev-ApiStack"
THING_TYPE = "interbridge-dev-device"
THING_GROUP = "interbridge-dev-devices"
POLICY = "interbridge-dev-device-policy"
MAX_COGNITO_SUB_LENGTH = 128


def valid_cognito_sub(value: object) -> TypeGuard[str]:
    """Apply only transport/storage safety checks to Cognito's opaque identifier."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_COGNITO_SUB_LENGTH
        and all(unicodedata.category(character) != "Cc" for character in value)
    )


class RegistrationError(RuntimeError):
    """A safety boundary failed before any write, or the atomic write conflicted."""


@dataclass(frozen=True)
class Resources:
    devices_table: str
    memberships_table: str
    user_pool_id: str


@dataclass
class Registrar:
    sts: Any
    cloudformation: Any
    cognito: Any
    iot: Any
    ddb: Any

    def _resources(self) -> Resources:
        def outputs(stack_name: str) -> dict[str, str]:
            response = self.cloudformation.describe_stacks(StackName=stack_name)
            stacks = response.get("Stacks", [])
            if len(stacks) != 1 or stacks[0].get("StackStatus") not in {
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
            }:
                raise RegistrationError("expected DEV stack is unavailable")
            return {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs", [])}

        data, api = outputs(DATA_STACK), outputs(API_STACK)
        expected_devices = "interbridge-dev-devices"
        expected_memberships = "interbridge-dev-device-memberships"
        if (
            data.get("DevicesTableName") != expected_devices
            or data.get("DeviceMembershipsTableName") != expected_memberships
        ):
            raise RegistrationError("DEV data stack outputs do not match expected resources")
        pool = api.get("UserPoolId")
        if not isinstance(pool, str) or not pool.startswith(f"{REGION}_"):
            raise RegistrationError("DEV API stack User Pool output is invalid")
        return Resources(expected_devices, expected_memberships, pool)

    def _validate_user(self, pool: str, sub: str) -> None:
        escaped = sub.replace("\\", "\\\\").replace('"', '\\"')
        response = self.cognito.list_users(UserPoolId=pool, Filter=f'sub = "{escaped}"', Limit=2)
        users = response.get("Users", [])
        if len(users) != 1:
            raise RegistrationError("Cognito user does not exist or is ambiguous")
        attributes = {a["Name"]: a["Value"] for a in users[0].get("Attributes", [])}
        if attributes.get("sub") != sub:
            raise RegistrationError("Cognito user identity mismatch")

    def _validate_thing(self, device_id: str) -> None:
        try:
            thing = self.iot.describe_thing(thingName=device_id)
        except self.iot.exceptions.ResourceNotFoundException as exc:
            raise RegistrationError("IoT Thing does not exist") from exc
        if thing.get("thingName") != device_id or thing.get("thingTypeName") != THING_TYPE:
            raise RegistrationError("IoT Thing identity or type mismatch")
        groups = self.iot.list_thing_groups_for_thing(thingName=device_id).get("thingGroups", [])
        if [group.get("groupName") for group in groups] != [THING_GROUP]:
            raise RegistrationError("IoT Thing group bindings are invalid")
        principals = self.iot.list_thing_principals(thingName=device_id).get("principals", [])
        if len(principals) != 1:
            raise RegistrationError("IoT Thing certificate binding is invalid")
        principal = str(principals[0])
        certificate_id = principal.rsplit("/", 1)[-1]
        certificate = self.iot.describe_certificate(certificateId=certificate_id).get(
            "certificateDescription", {}
        )
        if certificate.get("certificateArn") != principal or certificate.get("status") != "ACTIVE":
            raise RegistrationError("IoT certificate binding is invalid")
        policies = self.iot.list_attached_policies(target=principal).get("policies", [])
        if [policy.get("policyName") for policy in policies] != [POLICY]:
            raise RegistrationError("IoT policy binding is invalid")

    @staticmethod
    def _decode(item: dict[str, Any]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, attribute in item.items():
            if "S" in attribute:
                decoded[key] = attribute["S"]
            elif "N" in attribute:
                decoded[key] = int(attribute["N"])
            else:
                return {}
        return decoded

    @staticmethod
    def _encode(item: dict[str, object]) -> dict[str, dict[str, str]]:
        return {
            key: ({"N": str(value)} if isinstance(value, int) else {"S": str(value)})
            for key, value in item.items()
        }

    @staticmethod
    def _semantic_match(
        existing: dict[str, Any] | None, expected: dict[str, object], temporal: set[str]
    ) -> bool:
        if not existing:
            return False
        decoded = Registrar._decode(existing)
        if any(not isinstance(decoded.get(key), int) for key in temporal):
            return False
        return {k: v for k, v in decoded.items() if k not in temporal} == {
            k: v for k, v in expected.items() if k not in temporal
        }

    def register(
        self,
        *,
        environment: str,
        region: str,
        sub: str,
        device_id: str,
        hardware_version: str,
        manufacturing_batch: str,
        temporary_credentials: bool,
        dry_run: bool,
        confirmation: str | None = None,
        now: int | None = None,
    ) -> str:
        if environment != "dev" or region != REGION:
            raise RegistrationError("only dev in sa-east-1 is allowed")
        if not temporary_credentials:
            raise RegistrationError("temporary AWS credentials are required")
        if not valid_cognito_sub(sub):
            raise RegistrationError("invalid Cognito sub")
        try:
            validate_device_id(device_id)
        except ValueError as exc:
            raise RegistrationError("invalid device_id") from exc
        identity = self.sts.get_caller_identity()
        arn = str(identity.get("Arn", ""))
        if arn.endswith(":root") or ":assumed-role/" not in arn:
            raise RegistrationError("an assumed operator role is required")
        print(f"Operator role: {arn.split(':assumed-role/', 1)[-1].split('/', 1)[0]}")
        resources = self._resources()
        self._validate_user(resources.user_pool_id, sub)
        self._validate_thing(device_id)
        stamp = int(now if now is not None else time.time())
        device: dict[str, object] = {
            "device_id": device_id,
            "hardware_version": hardware_version,
            "manufacturing_batch": manufacturing_batch,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
            "aws_thing_name": device_id,
            "owner_user_id": sub,
            "created_at": stamp,
            "updated_at": stamp,
            "claimed_at": stamp,
            "version": 1,
        }
        membership: dict[str, object] = {
            "device_id": device_id,
            "user_id": sub,
            "role": "OWNER",
            "status": "ACTIVE",
            "created_at": stamp,
            "updated_at": stamp,
            "created_by": "DEV_ADMIN_TOOL",
            "version": 1,
        }
        phrase = f"REGISTER DEV {device_id} OWNER {sub}"
        if not dry_run and confirmation != phrase:
            raise RegistrationError("explicit confirmation did not match")
        if dry_run:
            return "DRY RUN validated; no registry writes performed"
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": resources.devices_table,
                            "Item": self._encode(device),
                            "ConditionExpression": "attribute_not_exists(device_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": resources.memberships_table,
                            "Item": self._encode(membership),
                            "ConditionExpression": (
                                "attribute_not_exists(device_id) AND attribute_not_exists(user_id)"
                            ),
                        }
                    },
                ]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            existing_device = self.ddb.get_item(
                TableName=resources.devices_table,
                Key={"device_id": {"S": device_id}},
                ConsistentRead=True,
            ).get("Item")
            existing_membership = self.ddb.get_item(
                TableName=resources.memberships_table,
                Key={"device_id": {"S": device_id}, "user_id": {"S": sub}},
                ConsistentRead=True,
            ).get("Item")
            if not self._semantic_match(
                existing_device, device, {"created_at", "updated_at", "claimed_at"}
            ) or not self._semantic_match(
                existing_membership, membership, {"created_at", "updated_at"}
            ):
                raise RegistrationError(
                    "ownership or metadata conflict; transaction aborted"
                ) from exc
            return "Device was already registered with identical semantic data"
        return "Device and OWNER membership registered atomically"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for name in (
        "environment",
        "region",
        "sub",
        "device-id",
        "hardware-version",
        "manufacturing-batch",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    try:
        import boto3

        session = boto3.Session(region_name=args.region)
        credentials = session.get_credentials()
        registrar = Registrar(
            session.client("sts"),
            session.client("cloudformation"),
            session.client("cognito-idp"),
            session.client("iot"),
            session.client("dynamodb"),
        )
        print(
            registrar.register(
                environment=args.environment,
                region=args.region,
                sub=args.sub,
                device_id=args.device_id,
                hardware_version=args.hardware_version,
                manufacturing_batch=args.manufacturing_batch,
                temporary_credentials=bool(credentials and credentials.token),
                dry_run=args.dry_run,
                confirmation=args.confirm,
            )
        )
    except RegistrationError as exc:
        print(f"Refused safely: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Operational failure; no diagnostic details were displayed.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
