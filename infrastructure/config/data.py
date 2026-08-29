"""Centralized DynamoDB table/index naming for the InterBridge backend.

This is the single source of truth for every table and index name used by
``DataStack`` (``infrastructure/stacks/data_stack.py``), mirroring how
``infrastructure/config/iot.py`` centralizes AWS IoT naming for
``IoTStack``.

The generic ``infrastructure.config.naming.resource_name()`` helper always
inserts a ``component`` segment (``interbridge-dev-<component>-<resource>``),
but Fase 1C's task brief specifies exact table names without a component
segment (e.g. ``interbridge-dev-devices``, not
``interbridge-dev-database-devices``). Rather than bend the generic helper
to a special case, this module -- like ``iot.py`` before it -- builds names
directly from ``EnvironmentConfig.project``/``environment`` so the
requested names match exactly, while still keeping every name in one
place instead of hand-typed at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.config.environment import EnvironmentConfig


@dataclass(frozen=True)
class DataNames:
    """Deterministic names for every DynamoDB table/index this stack owns."""

    devices_table_name: str
    setup_code_lookups_table_name: str
    device_memberships_table_name: str
    claim_sessions_table_name: str
    telemetry_table_name: str
    push_installations_table_name: str
    # Fase 3B.6/3B.7: sole authority for ring-delivery idempotency
    # (device_id + event_id), independent of any GSI's eventual
    # consistency -- see docs/fcm-notification-sender.md.
    push_deliveries_table_name: str
    # GSI on DeviceMemberships: list every device a user can access.
    memberships_by_user_index_name: str
    # GSI on ClaimSessions: list recent claim attempts for a device
    # without a table Scan.
    claim_sessions_by_device_index_name: str
    push_installations_by_user_index_name: str


def data_names(config: EnvironmentConfig) -> DataNames:
    """Build every DynamoDB table/index name from the shared environment config."""
    prefix = f"{config.project}-{config.environment}"
    return DataNames(
        devices_table_name=f"{prefix}-devices",
        setup_code_lookups_table_name=f"{prefix}-setup-code-lookups",
        device_memberships_table_name=f"{prefix}-device-memberships",
        claim_sessions_table_name=f"{prefix}-claim-sessions",
        telemetry_table_name=f"{prefix}-telemetry",
        push_installations_table_name=f"{prefix}-push-installations",
        push_deliveries_table_name=f"{prefix}-push-notification-deliveries",
        memberships_by_user_index_name=f"{prefix}-device-memberships-by-user-index",
        claim_sessions_by_device_index_name=f"{prefix}-claim-sessions-by-device-index",
        push_installations_by_user_index_name=f"{prefix}-push-installations-by-user-index",
    )
