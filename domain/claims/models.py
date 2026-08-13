"""The ``ClaimSession`` record: a short-lived, single-use onboarding authorization.

Mirrors the ``interbridge-dev-claim-sessions`` DynamoDB table (partition
key ``claim_session_id``, a ``<...>-by-device-index`` GSI keyed by
``device_id``/``created_at``, and DynamoDB TTL on the ``ttl`` attribute --
see ``infrastructure/stacks/data_stack.py`` and ``docs/data-model.md``).

See ``CONTEXT.md``, "Onboarding BLE-first" and
``docs/adr/0001-ble-first-onboarding.md`` for why a ``claim_session`` is
never a generic, reusable provisioning token, and why completion always
requires cloud-side confirmation -- never just the app's say-so.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from domain.claims.enums import TERMINAL_CLAIM_STATUSES, ClaimSource, ClaimStatus
from domain.devices.identifiers import validate_device_id


@dataclass(frozen=True)
class ClaimSession:
    """One onboarding attempt: a specific user claiming a specific device."""

    claim_session_id: str
    device_id: str
    user_id: str
    source: ClaimSource
    status: ClaimStatus
    created_at: int
    expires_at: int
    ttl: int
    used_at: int | None = None
    completed_at: int | None = None
    cancelled_at: int | None = None
    failure_code: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.claim_session_id.strip():
            raise ValueError("claim_session_id must not be empty.")
        validate_device_id(self.device_id)
        if not self.user_id.strip():
            raise ValueError("user_id must not be empty.")

        if self.created_at < 0:
            raise ValueError("created_at must be a non-negative Unix epoch second value.")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at.")
        # The DynamoDB `ttl` attribute drives automatic deletion and must
        # mirror expires_at exactly -- see the module/table docstrings for
        # why TTL deletion is not a substitute for an expires_at check at
        # read time.
        if self.ttl != self.expires_at:
            raise ValueError("ttl must equal expires_at.")

        if self.used_at is not None and self.used_at < self.created_at:
            raise ValueError("used_at must not be earlier than created_at.")

        self._validate_status_timestamp_consistency()

        if self.version < 1:
            raise ValueError("version must be a positive integer (optimistic concurrency).")

    def _validate_status_timestamp_consistency(self) -> None:
        status = self.status

        if status == ClaimStatus.COMPLETED:
            if self.completed_at is None:
                raise ValueError("completed_at is required when status is COMPLETED.")
            if self.cancelled_at is not None or self.failure_code is not None:
                raise ValueError("COMPLETED sessions must not have cancelled_at or failure_code.")
        elif status == ClaimStatus.CANCELLED:
            if self.cancelled_at is None:
                raise ValueError("cancelled_at is required when status is CANCELLED.")
            if self.completed_at is not None or self.failure_code is not None:
                raise ValueError("CANCELLED sessions must not have completed_at or failure_code.")
        elif status == ClaimStatus.FAILED:
            if not self.failure_code:
                raise ValueError("failure_code is required when status is FAILED.")
            if self.completed_at is not None or self.cancelled_at is not None:
                raise ValueError("FAILED sessions must not have completed_at or cancelled_at.")
        else:
            # PENDING, AUTHORIZED, PROVISIONING, EXPIRED: none of the
            # terminal-outcome fields may be set yet.
            if self.completed_at is not None:
                raise ValueError(f"completed_at must not be set while status is {status.value}.")
            if self.cancelled_at is not None:
                raise ValueError(f"cancelled_at must not be set while status is {status.value}.")
            if self.failure_code is not None:
                raise ValueError(f"failure_code must not be set while status is {status.value}.")

        # PENDING means "not yet used"; AUTHORIZED/PROVISIONING imply the
        # session has been used at least once.
        if status == ClaimStatus.PENDING and self.used_at is not None:
            raise ValueError("used_at must not be set while status is PENDING.")
        if (
            status not in TERMINAL_CLAIM_STATUSES
            and status != ClaimStatus.PENDING
            and self.used_at is None
        ):
            raise ValueError(f"used_at is required once status reaches {status.value}.")

    def to_item(self) -> dict[str, object]:
        """Render as a plain dict suitable for a DynamoDB item.

        Never includes a raw setup_code -- there is no such field on this
        model; only ``device_id`` (already resolved) is stored.
        """
        item: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            item[f.name] = value.value if isinstance(value, (ClaimSource, ClaimStatus)) else value
        return item
