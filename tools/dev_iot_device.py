"""Safely manage one disposable InterBridge DEV AWS IoT identity.

AWS calls occur only from :func:`main`/``DevIotDeviceTool`` operations. Importing this
module is side-effect free, which keeps tests and CDK synthesis offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REGION = "sa-east-1"
THING_TYPE = "interbridge-dev-device"
THING_GROUP = "interbridge-dev-devices"
POLICY = "interbridge-dev-device-policy"
DEVICE_RE = re.compile(r"ib-[0-9a-f]{32}\Z")
CERTIFICATE_FILE = "device-certificate.pem.crt"
PRIVATE_KEY_FILE = "private.pem.key"
ROOT_CA_FILE = "AmazonRootCA1.pem"
ENDPOINT_FILE = "endpoint.txt"
METADATA_FILE = "device-metadata.json"
LOG = logging.getLogger(__name__)
CHECKOUT_MARKERS = ("CONTEXT.md", "README.md", "pyproject.toml")


class SafetyError(RuntimeError):
    """An invariant failed; no aggressive recovery should be attempted."""


class MissingCrtDependencyError(RuntimeError):
    """The AWS credential provider in use needs the optional CRT dependency.

    Newer boto3/botocore credential providers -- notably the AWS CLI
    "login" provider -- require ``awscrt``, which ships as the ``crt``
    extra (``boto3[crt]``/``botocore[crt]``), not as a base dependency.
    Without it, boto3 raises ``botocore.exceptions.MissingDependencyException``
    while building a client. That exception's own message is accurate but
    verbose; this wraps it into one short, actionable line instead of
    letting a full traceback reach the operator's terminal.
    """


def discover_checkout_root(module_path: Path | None = None) -> Path:
    """Find the real Git checkout containing this module, independent of cwd.

    A ``.git`` entry alone is not sufficient because an unrelated parent repository could
    otherwise be accepted. Project markers and this module's expected path must all be present.
    ``.git`` may be either a directory or a file (as used by Git worktrees).
    """
    try:
        source = (module_path or Path(__file__)).resolve(strict=True)
    except OSError as exc:
        raise SafetyError("could not safely determine the InterBridge Git checkout root") from exc
    start = source.parent if source.is_file() else source
    for candidate in (start, *start.parents):
        git_entry = candidate / ".git"
        expected_module = candidate / "tools" / "dev_iot_device.py"
        if (
            (git_entry.is_dir() or git_entry.is_file())
            and expected_module.is_file()
            and expected_module.resolve(strict=True) == source
            and all((candidate / marker).is_file() for marker in CHECKOUT_MARKERS)
        ):
            return candidate.resolve(strict=True)
    raise SafetyError("could not safely determine the InterBridge Git checkout root")


def validate_device_id(value: str) -> str:
    if not DEVICE_RE.fullmatch(value):
        raise SafetyError("device_id must be ib- followed by 32 lowercase hexadecimal characters")
    return value


def validate_region(value: str) -> str:
    if value != REGION:
        raise SafetyError(f"region must be explicitly set to {REGION}")
    return value


def validate_output_dir(value: Path, checkout: Path) -> Path:
    output = value.expanduser().resolve()
    root = checkout.resolve()
    if output == root or root in output.parents:
        raise SafetyError("output directory must be outside the Git checkout")
    return output


def _write_private(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Identity:
    account: str
    arn: str


class DevIotDeviceTool:
    def __init__(self, sts: Any, iot: Any, *, checkout: Path) -> None:
        self.sts = sts
        self.iot = iot
        self.checkout = checkout

    def identity(self) -> Identity:
        response = self.sts.get_caller_identity()
        identity = Identity(str(response["Account"]), str(response["Arn"]))
        if identity.arn.endswith(":root"):
            raise SafetyError("AWS account root user is forbidden; use a short-lived operator role")
        return identity

    @staticmethod
    def _confirm(
        action: str,
        device_id: str,
        identity: Identity,
        region: str,
        supplied: str | None,
        input_fn: Callable[[str], str],
    ) -> None:
        phrase = f"{action.upper()} {device_id}"
        print(f"AWS account: {identity.account}\nRegion: {region}\nDevice: {device_id}")
        answer = supplied if supplied is not None else input_fn(f"Type '{phrase}' to continue: ")
        if answer != phrase:
            raise SafetyError("explicit confirmation did not match")

    def provision(
        self,
        device_id: str,
        region: str,
        output_dir: Path,
        *,
        dry_run: bool,
        confirmation: str | None = None,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        validate_device_id(device_id)
        validate_region(region)
        output = validate_output_dir(output_dir, self.checkout)
        identity = self.identity()  # STS always precedes any write.
        self._confirm("provision", device_id, identity, region, confirmation, input_fn)
        if dry_run:
            print(
                "DRY RUN: would create Thing, group membership, unique certificate and attachments"
            )
            return
        if output.exists() and any(output.iterdir()):
            raise SafetyError(
                "output directory must be absent or empty; existing credentials are never replaced"
            )
        try:
            self.iot.describe_thing(thingName=device_id)
        except self.iot.exceptions.ResourceNotFoundException:
            pass
        else:
            raise SafetyError("Thing already exists; refusing to reuse it or its certificate")

        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output, 0o700)
        # Deliberately ordered so identity/container constraints precede certificate issuance.
        self.iot.create_thing(thingName=device_id, thingTypeName=THING_TYPE)
        self.iot.add_thing_to_thing_group(thingName=device_id, thingGroupName=THING_GROUP)
        certificate = self.iot.create_keys_and_certificate(setAsActive=True)
        certificate_arn = str(certificate["certificateArn"])
        self.iot.attach_thing_principal(thingName=device_id, principal=certificate_arn)
        self.iot.attach_policy(policyName=POLICY, target=certificate_arn)
        verified_arn, verified_id = self._inspect(device_id)
        if (verified_arn, verified_id) != (certificate_arn, str(certificate["certificateId"])):
            raise SafetyError("post-provision bindings do not match the newly issued certificate")
        endpoint = str(self.iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"])

        # Secrets are held only long enough to write owner-only local files and are never logged.
        _write_private(output / CERTIFICATE_FILE, str(certificate["certificatePem"]))
        _write_private(output / PRIVATE_KEY_FILE, str(certificate["keyPair"]["PrivateKey"]))
        _write_private(output / ENDPOINT_FILE, endpoint + "\n")
        metadata = {
            "device_id": device_id,
            "thing_name": device_id,
            "certificate_id": certificate["certificateId"],
            "certificate_arn": certificate_arn,
            "endpoint": endpoint,
            "region": region,
            "created_at": datetime.now(UTC).isoformat(),
            "policy_name": POLICY,
            "thing_group_name": THING_GROUP,
            "thing_type_name": THING_TYPE,
        }
        _write_private(output / METADATA_FILE, json.dumps(metadata, indent=2) + "\n")
        print(
            f"Provisioned safely; credentials written to {output}. "
            "Root CA still requires manual download."
        )

    def _inspect(self, device_id: str) -> tuple[str, str]:
        thing = self.iot.describe_thing(thingName=device_id)
        if thing.get("thingName") != device_id or thing.get("thingTypeName") != THING_TYPE:
            raise SafetyError("Thing identity/type does not exactly match the requested DEV device")
        groups = self.iot.list_thing_groups_for_thing(thingName=device_id).get("thingGroups", [])
        if [g.get("groupName") for g in groups] != [THING_GROUP]:
            raise SafetyError("Thing has missing or unexpected group memberships")
        principals = self.iot.list_thing_principals(thingName=device_id).get("principals", [])
        if len(principals) != 1:
            raise SafetyError(
                "expected exactly one certificate principal; manual intervention required"
            )
        arn = str(principals[0])
        certificate_id = arn.rsplit("/", 1)[-1]
        detail = self.iot.describe_certificate(certificateId=certificate_id)[
            "certificateDescription"
        ]
        if detail.get("certificateArn") != arn:
            raise SafetyError("certificate ARN does not correspond exactly to the Thing principal")
        if detail.get("status") != "ACTIVE":
            raise SafetyError("certificate is not ACTIVE; manual intervention required")
        policies = self.iot.list_attached_policies(target=arn).get("policies", [])
        if [p.get("policyName") for p in policies] != [POLICY]:
            raise SafetyError(
                "certificate has missing or unexpected policies; manual intervention required"
            )
        return arn, certificate_id

    def verify(self, device_id: str, region: str) -> None:
        validate_device_id(device_id)
        validate_region(region)
        identity = self.identity()
        arn, certificate_id = self._inspect(device_id)
        endpoint = self.iot.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
        print(
            f"Verified account={identity.account} region={region} device={device_id} "
            f"certificate_id={certificate_id} endpoint={endpoint} principal={arn}"
        )

    def cleanup(
        self,
        device_id: str,
        region: str,
        *,
        dry_run: bool,
        confirmation: str | None = None,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        validate_device_id(device_id)
        validate_region(region)
        identity = self.identity()
        arn, certificate_id = self._inspect(device_id)  # All boundaries verified before mutation.
        self._confirm("cleanup", device_id, identity, region, confirmation, input_fn)
        if dry_run:
            print(
                "DRY RUN: verified exact DEV bindings; would detach, deactivate "
                "and delete in safe order"
            )
            return
        self.iot.detach_policy(policyName=POLICY, target=arn)
        self.iot.detach_thing_principal(thingName=device_id, principal=arn)
        self.iot.update_certificate(certificateId=certificate_id, newStatus="INACTIVE")
        self.iot.delete_certificate(certificateId=certificate_id, forceDelete=False)
        self.iot.remove_thing_from_thing_group(thingName=device_id, thingGroupName=THING_GROUP)
        self.iot.delete_thing(thingName=device_id)
        print(f"Cleaned up DEV device {device_id}")


def build_tool(region: str, checkout: Path) -> DevIotDeviceTool:
    """Construct the boto3 sts/iot clients and wrap them in a ``DevIotDeviceTool``.

    Isolated from :func:`main` so the CRT-dependency error handling below is
    unit-testable without a real (or even installed) boto3/botocore stack --
    see ``tests/unit/test_dev_iot_device.py``.
    """
    import boto3
    from botocore.exceptions import MissingDependencyException

    try:
        session = boto3.Session(region_name=region)
        sts = session.client("sts")
        iot = session.client("iot")
    except MissingDependencyException as exc:
        raise MissingCrtDependencyError(
            "Missing optional dependency for the AWS credential provider in use "
            "(e.g. the AWS CLI 'login' provider needs botocore[crt]). "
            "Run: python -m pip install -r requirements-tools.txt"
        ) from exc
    return DevIotDeviceTool(sts, iot, checkout=checkout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("provision", "verify", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("--device-id", required=True)
        command.add_argument("--region", required=True)
        if name != "verify":
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--confirm", help="exact non-interactive confirmation phrase")
        if name == "provision":
            command.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checkout = discover_checkout_root()
        region = validate_region(args.region)
        tool = build_tool(region, checkout)
        if args.operation == "provision":
            tool.provision(
                args.device_id,
                args.region,
                args.output_dir,
                dry_run=args.dry_run,
                confirmation=args.confirm,
            )
        elif args.operation == "verify":
            tool.verify(args.device_id, args.region)
        else:
            tool.cleanup(
                args.device_id, args.region, dry_run=args.dry_run, confirmation=args.confirm
            )
    except MissingCrtDependencyError as exc:
        # Short and actionable, deliberately with no traceback: this is a
        # local environment/setup problem, not an AWS error to investigate.
        print(str(exc), file=sys.stderr)
        return 3
    except (SafetyError, KeyboardInterrupt) as exc:
        print(f"Refused safely: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
