"""DataStack: DynamoDB persistence layer for the InterBridge backend.

Fase 1C scope (this phase) -- device registry, ownership and claim-session
storage, with no runtime consumer yet:

- ``Devices`` table: one item per physical InterBridge device (manufacturing
  identity, ownership/provisioning status). Partition key ``device_id``.
- ``SetupCodeLookups`` table: exact-match lookup from a ``setup_code``
  digest to a ``device_id``. Partition key ``setup_code_digest``. The raw
  ``setup_code`` is never stored -- see
  ``domain/claims/setup_code.py`` for the HMAC-SHA256 digest algorithm.
- ``DeviceMemberships`` table: user <-> device ownership/membership
  records, separate from device identity so a device's physical identity
  never depends on who currently owns it. Partition key ``device_id``,
  sort key ``user_id``, plus a GSI to list a user's devices.
- ``ClaimSessions`` table: short-lived, single-use onboarding
  authorizations (see ``docs/adr/0001-ble-first-onboarding.md`` and
  ``CONTEXT.md``, "Onboarding BLE-first"). Partition key
  ``claim_session_id``, plus a GSI to list a device's recent claim
  attempts, and DynamoDB TTL on the ``ttl`` attribute.

Every table uses on-demand billing (``PAY_PER_REQUEST``), AWS-owned
encryption (no customer-managed KMS key), deletion protection, and
``RemovalPolicy.RETAIN`` -- see the module docstring in
``docs/data-model.md`` for the full rationale, including what ``RETAIN``
+ deletion protection mean for tearing down the DEV environment later
(the tables must be emptied and explicitly deleted; `cdk destroy` alone
will not remove them).

This phase intentionally does **not** create:

- Any Lambda function, API Gateway route, or Cognito resource (no runtime
  consumer exists yet -- see ``domain/`` for the framework-independent
  models/validators that a future consumer will use).
- Any IAM policy granting access to these tables (see ``docs/data-model.md``
  for the documented future minimum-privilege roles).
- Any DynamoDB Stream, Global Table, customer-managed KMS key, or Secrets
  Manager resource (the HMAC pepper for setup-code digests is deliberately
  not provisioned yet -- see ``domain/claims/setup_code.py``).
- Any seed data or example device.

Depends on (future): none yet. ``IoTStack`` and ``ApiStack`` will depend on
table ARNs exported from here once a runtime consumer exists (Fase 1E/2),
never the other way around, to avoid a circular dependency.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb

from constructs import Construct
from infrastructure.config.data import DataNames, data_names
from infrastructure.config.environment import EnvironmentConfig


class DataStack(Stack):
    """Owns the persistence layer (DynamoDB tables) for InterBridge.

    No other stack should provision its own data storage: ``IoTStack`` and
    ``ApiStack`` will depend on table references exported from here once a
    runtime consumer is implemented, avoiding circular dependencies.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.names: DataNames = data_names(config)

        for key, value in config.standard_tags.items():
            Tags.of(self).add(key, value)
        for key, value in config.component_tag("database").items():
            Tags.of(self).add(key, value)

        self.devices_table = dynamodb.Table(
            self,
            "DevicesTable",
            table_name=self.names.devices_table_name,
            partition_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            **self._common_table_kwargs(),
        )

        self.setup_code_lookups_table = dynamodb.Table(
            self,
            "SetupCodeLookupsTable",
            table_name=self.names.setup_code_lookups_table_name,
            partition_key=dynamodb.Attribute(
                name="setup_code_digest", type=dynamodb.AttributeType.STRING
            ),
            **self._common_table_kwargs(),
        )

        self.device_memberships_table = dynamodb.Table(
            self,
            "DeviceMembershipsTable",
            table_name=self.names.device_memberships_table_name,
            partition_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            **self._common_table_kwargs(),
        )
        # Access pattern: list every device a given user can access,
        # without a table Scan.
        self.device_memberships_table.add_global_secondary_index(
            index_name=self.names.memberships_by_user_index_name,
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.claim_sessions_table = dynamodb.Table(
            self,
            "ClaimSessionsTable",
            table_name=self.names.claim_sessions_table_name,
            partition_key=dynamodb.Attribute(
                name="claim_session_id", type=dynamodb.AttributeType.STRING
            ),
            # Unix epoch seconds; DynamoDB TTL deletion is asynchronous and
            # is not a substitute for validating expires_at against the
            # backend's own clock at read time -- see docs/data-model.md.
            time_to_live_attribute="ttl",
            **self._common_table_kwargs(),
        )
        # Access pattern: list a device's recent claim attempts without a
        # table Scan. A GSI partitioned only by `status` was deliberately
        # rejected -- see docs/data-model.md -- because `status` alone is a
        # low-cardinality key shared by every session in that state.
        self.claim_sessions_table.add_global_secondary_index(
            index_name=self.names.claim_sessions_by_device_index_name,
            partition_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.NUMBER),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.telemetry_table = dynamodb.Table(
            self,
            "TelemetryTable",
            table_name=self.names.telemetry_table_name,
            partition_key=dynamodb.Attribute(name="device_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="record_key", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            **self._common_table_kwargs(),
        )

    @staticmethod
    def _common_table_kwargs() -> dict[str, Any]:
        """Shared, deliberately conservative configuration for every table.

        - ``PAY_PER_REQUEST``: no provisioned capacity, no autoscaling.
        - ``TableEncryption.DEFAULT``: server-side encryption with a key
          owned by AWS -- never a customer-managed KMS key (that would add
          cost and operational surface before any consumer exists).
        - ``point_in_time_recovery_specification`` disabled: acceptable
          for DEV, where tables hold no data worth restoring yet; revisit
          before a real launch.
        - ``deletion_protection=True`` + ``RemovalPolicy.RETAIN``: a table
          cannot be deleted by `cdk destroy`/console accident. Tearing down
          DEV later requires explicitly disabling deletion protection and
          deleting each table by hand -- see docs/data-model.md.
        - No ``stream`` and no ``replication_regions``: no DynamoDB Streams,
          no Global Tables.
        """
        return {
            "billing_mode": dynamodb.BillingMode.PAY_PER_REQUEST,
            "encryption": dynamodb.TableEncryption.DEFAULT,
            "point_in_time_recovery_specification": dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False
            ),
            "deletion_protection": True,
            "removal_policy": RemovalPolicy.RETAIN,
        }
