"""DEV-only post-deploy registry operation; never run as part of CI/deploy."""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from domain.devices.enums import OwnershipStatus, ProvisioningStatus
from domain.devices.identifiers import validate_device_id
from domain.devices.models import Device
from domain.ownership.enums import MembershipRole, MembershipStatus
from domain.ownership.models import DeviceMembership

REGION = "sa-east-1"
SUB = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)


class RegistrationError(RuntimeError):
    pass


@dataclass
class Registrar:
    sts: Any
    cognito: Any
    iot: Any
    ddb: Any
    devices_table: str
    memberships_table: str
    user_pool_id: str

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
        if not SUB.fullmatch(sub):
            raise RegistrationError("invalid Cognito sub")
        try:
            validate_device_id(device_id)
        except ValueError as exc:
            raise RegistrationError("invalid device_id") from exc
        identity = self.sts.get_caller_identity()
        arn = str(identity.get("Arn", ""))
        if arn.endswith(":root"):
            raise RegistrationError("root identity is forbidden")
        print(
            f"Operator role: {arn.rsplit('/', 1)[-1]} "
            f"(account ending {str(identity.get('Account', ''))[-4:]})"
        )
        try:
            user = self.cognito.admin_get_user(UserPoolId=self.user_pool_id, Username=sub)
        except self.cognito.exceptions.UserNotFoundException as exc:
            raise RegistrationError("Cognito user does not exist") from exc
        attrs = {a["Name"]: a["Value"] for a in user.get("UserAttributes", [])}
        if attrs.get("sub") != sub:
            raise RegistrationError("Cognito user identity mismatch")
        try:
            thing = self.iot.describe_thing(thingName=device_id)
        except self.iot.exceptions.ResourceNotFoundException as exc:
            raise RegistrationError("IoT Thing does not exist") from exc
        if thing.get("thingName") != device_id:
            raise RegistrationError("IoT Thing identity mismatch")
        stamp = int(now if now is not None else time.time())
        device = Device(
            device_id,
            hardware_version,
            manufacturing_batch,
            OwnershipStatus.OWNED,
            ProvisioningStatus.PROVISIONED,
            device_id,
            stamp,
            stamp,
            claimed_at=stamp,
        ).to_item()
        device["owner_user_id"] = sub
        membership = DeviceMembership(
            device_id,
            sub,
            MembershipRole.OWNER,
            MembershipStatus.ACTIVE,
            stamp,
            stamp,
            "DEV_ADMIN_TOOL",
        ).to_item()
        phrase = f"REGISTER DEV {device_id} OWNER {sub}"
        if not dry_run and confirmation != phrase:
            raise RegistrationError(f"type the exact confirmation phrase: {phrase}")
        if dry_run:
            return "DRY RUN validated; no registry writes performed"
        from boto3.dynamodb.types import TypeSerializer

        ser = TypeSerializer()

        def encode(item: dict[str, object]) -> dict[str, object]:
            return {k: ser.serialize(v) for k, v in item.items()}

        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.devices_table,
                            "Item": encode(device),
                            "ConditionExpression": "attribute_not_exists(device_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.memberships_table,
                            "Item": encode(membership),
                            "ConditionExpression": (
                                "attribute_not_exists(device_id) AND attribute_not_exists(user_id)"
                            ),
                        }
                    },
                ]
            )
        except self.ddb.exceptions.TransactionCanceledException as exc:
            existing_device = self.ddb.get_item(
                TableName=self.devices_table,
                Key={"device_id": {"S": device_id}},
                ConsistentRead=True,
            ).get("Item")
            existing_member = self.ddb.get_item(
                TableName=self.memberships_table,
                Key={"device_id": {"S": device_id}, "user_id": {"S": sub}},
                ConsistentRead=True,
            ).get("Item")
            if existing_device != encode(device) or existing_member != encode(membership):
                raise RegistrationError(
                    "ownership or metadata conflict; transaction aborted"
                ) from exc
            return "Device was already registered with identical data (idempotent retry)"
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
        "devices-table",
        "memberships-table",
        "user-pool-id",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    import boto3

    session = boto3.Session(region_name=args.region)
    creds = session.get_credentials()
    tool = Registrar(
        session.client("sts"),
        session.client("cognito-idp"),
        session.client("iot"),
        session.client("dynamodb"),
        args.devices_table,
        args.memberships_table,
        args.user_pool_id,
    )
    try:
        print(
            tool.register(
                environment=args.environment,
                region=args.region,
                sub=args.sub,
                device_id=args.device_id,
                hardware_version=args.hardware_version,
                manufacturing_batch=args.manufacturing_batch,
                temporary_credentials=bool(creds and creds.token),
                dry_run=args.dry_run,
                confirmation=args.confirm,
            )
        )
    except RegistrationError as exc:
        print(f"Refused safely: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
