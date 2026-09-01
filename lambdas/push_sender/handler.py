"""Lambda entry point for the FCM push sender (Fase 3B.6/3B.7).

Orchestrates -- and only orchestrates -- the components below. See
``docs/fcm-notification-sender.md`` for the full architecture and the
partial-failure semantics this function implements.

1. ``event.parse_invocation``            -- validate the invocation
2. ``idempotency.claim``/``.complete``/``.abandon`` -- authoritative dedup
3. ``memberships.active_memberships``    -- who has access to this device
4. ``lambdas.device_api.notification_preferences.combine`` +
   ``domain.push.preferences.evaluate``  -- what each member should get
5. ``installations.active_installations`` -- where to deliver it
6. ``domain.push.payload.compose_message`` -- what to send
7. ``fcm_client.send_with_retry``        -- how to send it (with a small,
   bounded local retry for RATE_LIMITED/TEMPORARY_ERROR)
8. ``domain.push.fcm_result.classify``    -- (done inside ``fcm_client``)
9. ``cleanup.delete_invalid_installation`` -- safe token removal
10. ``metrics.emit`` / structured logs    -- observability

Invoked asynchronously (``InvocationType="Event"``) by
``lambdas/telemetry_ingestion/handler.py`` -- never by API Gateway, never
directly by AWS IoT.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from domain.push.payload import compose_message
from domain.push.preferences import Decision, evaluate
from domain.push.temporal_eligibility import evaluate_temporal_eligibility
from lambdas.device_api.notification_preferences import combine
from lambdas.push_sender import cleanup, idempotency, memberships, metrics
from lambdas.push_sender.event import InvalidInvocation, parse_invocation
from lambdas.push_sender.fcm_client import FcmClient, send_with_retry
from lambdas.push_sender.firebase_auth import FirebaseCredentialError, TokenProvider
from lambdas.push_sender.installations import active_installations

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)


class UnresolvedTemporaryFailure(RuntimeError):
    """At least one installation's RATE_LIMITED/TEMPORARY_ERROR outcome
    never resolved after fcm_client.send_with_retry()'s local retries.
    Treated the same as a systemic auth failure: the attempt is abandoned
    (lambdas/push_sender/idempotency.abandon()) so the very next retry can
    reach every installation again, at the documented cost of a possible
    duplicate to installations already reached this attempt -- see
    docs/fcm-notification-sender.md, section 3.
    """


_ddb: Any = None
_secrets: Any = None
_token_provider: TokenProvider | None = None


def _default_clients() -> tuple[Any, Any]:
    global _ddb, _secrets
    if _ddb is None or _secrets is None:
        import boto3

        _ddb = _ddb or boto3.client("dynamodb")
        _secrets = _secrets or boto3.client("secretsmanager")
    return _ddb, _secrets


def _default_fcm_client(secrets_client: Any) -> FcmClient:
    global _token_provider
    if _token_provider is None:
        _token_provider = TokenProvider(
            secrets_client, os.environ["FIREBASE_CREDENTIALS_SECRET_NAME"]
        )
    import requests

    return FcmClient(
        project_id=_token_provider.project_id,
        token_source=_token_provider,
        session=requests.Session(),
    )


def _counters() -> dict[str, int]:
    return {
        "membership_count": 0,
        "installation_count": 0,
        "sent_count": 0,
        "suppressed_count": 0,
        "invalid_token_count": 0,
        "temporary_failure_count": 0,
        "permanent_failure_count": 0,
        "auth_config_failure_count": 0,
        "expired_suppressed_count": 0,
        "unknown_event_time_suppressed_count": 0,
        "future_event_time_suppressed_count": 0,
    }


def lambda_handler(
    payload: object,
    context: object,
    *,
    ddb: Any = None,
    fcm_client: Any = None,
    secrets_client: Any = None,
    clock: Any = time.time,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    del context
    try:
        ring_event = parse_invocation(payload)
    except InvalidInvocation as error:
        LOG.warning(json.dumps({"event": "push_sender_rejected", "reason": str(error)}))
        metrics.emit({"EventsRejected": 1})
        return {"result": "rejected", "reason": str(error)}

    metrics.emit({"EventsReceived": 1})
    ddb = ddb if ddb is not None else _default_clients()[0]
    deliveries_table = os.environ["PUSH_DELIVERIES_TABLE"]

    claim_outcome, attempt = idempotency.claim(
        ddb, deliveries_table, ring_event.device_id, ring_event.event_id, now=int(clock())
    )
    if claim_outcome in ("DUPLICATE_COMPLETED", "DUPLICATE_IN_FLIGHT"):
        metrics.emit({"EventsDuplicate": 1}, claim_outcome=claim_outcome)
        return {"result": "duplicate", "claim_outcome": claim_outcome}
    # claim_outcome is "CLAIMED" or "RESUMED" (a lease taken over after the
    # previous attempt crashed or ran past LEASE_SECONDS) -- both proceed
    # with a full fan-out. See lambdas/push_sender/idempotency.py for why a
    # RESUMED attempt can, rarely, re-notify an installation the crashed
    # attempt already reached.
    if claim_outcome == "RESUMED":
        LOG.warning(json.dumps({"event": "push_sender_lease_resumed", "attempt": attempt}))

    counters = _counters()
    now_utc = datetime.fromtimestamp(int(clock()), UTC)

    active, memberships_truncated = memberships.active_memberships(
        ddb, os.environ["MEMBERSHIPS_TABLE"], ring_event.device_id
    )
    counters["membership_count"] = len(active)
    if memberships_truncated:
        LOG.warning(json.dumps({"event": "push_sender_memberships_truncated"}))

    decisions: dict[str, Any] = {}
    for membership in active:
        user_id = membership.get("user_id")
        if not isinstance(user_id, str):
            continue
        raw_preferences = membership.get("notification_preferences")
        decisions[user_id] = (
            Decision("RING_ONLY", False, False, False, None)
            if ring_event.event == "RING_ENDED"
            else _decide(ring_event.event, raw_preferences, now_utc)
        )

    deliverable_user_ids = [
        user_id for user_id, decision in decisions.items() if not decision.suppressed
    ]
    counters["suppressed_count"] = len(decisions) - len(deliverable_user_ids)

    # Preserve preference semantics: NONE/all-suppressed remains a preference
    # outcome. Temporal eligibility only matters when a push would otherwise
    # be deliverable, and is evaluated before installations, Firebase secrets,
    # tokens or FCM are touched.
    if deliverable_user_ids:
        temporal = evaluate_temporal_eligibility(
            ring_event.event,
            ring_event.occurred_at,
            ring_event.timestamp_source,
            now=now_utc,
        )
        if not temporal.eligible:
            return _complete_temporal_suppression(
                ddb,
                deliveries_table,
                ring_event,
                attempt,
                counters,
                temporal.reason,
                temporal.age_bucket,
                now=int(clock()),
            )

    # Absence of members, or of every deliverable member's installations,
    # is a normal, valid outcome -- not an error -- so it still completes
    # the idempotency record with an accurate zero-work counter set rather
    # than short-circuiting as a failure.
    outcome = "processed"
    if not active:
        outcome = "no_recipients"
    elif not deliverable_user_ids:
        outcome = "all_suppressed"
    else:
        installations_list, installations_truncated = active_installations(
            ddb,
            os.environ["PUSH_INSTALLATIONS_TABLE"],
            os.environ["PUSH_INSTALLATIONS_BY_USER_INDEX"],
            deliverable_user_ids,
        )
        counters["installation_count"] = len(installations_list)
        if installations_truncated:
            LOG.warning(json.dumps({"event": "push_sender_installations_truncated"}))

        if not installations_list:
            outcome = "no_installations"
        else:
            # Lazy by construction: when a caller injects fcm_client (every
            # test in this repo, and nothing else), no Secrets Manager
            # client, no TokenProvider and no `requests` import ever
            # happen -- resolving _default_clients()[1] unconditionally
            # here was exactly the bug that made the test suite reach out
            # to real AWS (NoRegionError in CI, which has no region
            # configured on purpose). See
            # tests/unit/test_push_sender_handler.py's
            # "never_triggers_default_client_construction" test.
            if fcm_client is not None:
                fcm = fcm_client
            else:
                secrets_client = (
                    secrets_client if secrets_client is not None else _default_clients()[1]
                )
                fcm = _default_fcm_client(secrets_client)
            firebase_broken, unresolved_temporary_failure = _send_all(
                fcm,
                installations_list,
                decisions,
                ring_event,
                counters,
                ddb,
                os.environ["PUSH_INSTALLATIONS_TABLE"],
                now_utc=now_utc,
                sleeper=sleeper,
            )
            if firebase_broken or unresolved_temporary_failure:
                # Both are systemic-enough failures that this attempt must
                # not be allowed to complete: a total auth/config failure
                # affects every remaining installation, and an unresolved
                # temporary failure means at least one installation was
                # never actually reached even though the fan-out "ran to
                # completion". Marking COMPLETED here -- even with some
                # earlier sent_count > 0 -- would silently and permanently
                # drop whichever installations never got a real attempt.
                # abandon() releases the lease immediately (rather than
                # waiting up to LEASE_SECONDS) so the very next retry can
                # resume without delay; see
                # lambdas/push_sender/idempotency.py for the full timing
                # analysis and the documented at-least-once tradeoff this
                # implies (a retry re-runs the whole fan-out, so an
                # installation already reached this attempt may, rarely,
                # be notified twice).
                idempotency.abandon(
                    ddb,
                    deliveries_table,
                    ring_event.device_id,
                    ring_event.event_id,
                    now=int(clock()),
                    attempt=attempt,
                )
                if firebase_broken:
                    raise FirebaseCredentialError(
                        "Firebase credentials unusable for this invocation"
                    )
                raise UnresolvedTemporaryFailure(
                    "A temporary FCM failure did not resolve after local retries"
                )

    idempotency.complete(
        ddb,
        deliveries_table,
        ring_event.device_id,
        ring_event.event_id,
        now=int(clock()),
        attempt=attempt,
        counters=counters,
        outcome=outcome.upper(),
    )
    metrics.emit(
        {
            "EventsProcessed": 1,
            (
                "RingEndedAccepted" if ring_event.event == "RING_ENDED" else "RingDetectedAccepted"
            ): 1,
            "MembershipsFound": counters["membership_count"],
            "InstallationsFound": counters["installation_count"],
            "Sent": counters["sent_count"],
            "Suppressed": counters["suppressed_count"],
            "InvalidTokens": counters["invalid_token_count"],
            "TemporaryFailures": counters["temporary_failure_count"],
            "PermanentFailures": counters["permanent_failure_count"],
            "AuthConfigFailures": counters["auth_config_failure_count"],
        }
    )
    LOG.info(json.dumps({"event": "push_sender_completed", "outcome": outcome, **counters}))
    return {"result": outcome, **counters}


def _decide(event: str, raw_preferences: object, now_utc: datetime) -> Any:
    try:
        normalized = combine(raw_preferences)
    except (ValueError, TypeError):
        normalized = combine(None)
    try:
        return evaluate(event, normalized, now=now_utc)
    except ValueError:
        # A structurally valid-per-combine() but semantically broken
        # schedule (should not happen -- combine() validates it -- but
        # never let one member's malformed data suppress everyone else's
        # fan-out) falls back to the same v1 defaults absent preferences
        # use.
        return evaluate(event, combine(None), now=now_utc)


def _send_all(
    fcm: Any,
    installations_list: list[dict[str, Any]],
    decisions: dict[str, Any],
    ring_event: Any,
    counters: dict[str, int],
    ddb: Any,
    installations_table: str,
    *,
    now_utc: datetime,
    sleeper: Any = time.sleep,
) -> tuple[bool, bool]:
    """Returns ``(firebase_broken, unresolved_temporary_failure)``.

    ``firebase_broken`` stops all further sends this invocation (the
    access token itself is bad -- trying more installations would just
    waste calls). ``unresolved_temporary_failure`` does **not** stop
    further sends (a rate limit or temporary error on one installation
    says nothing about the others), but still tells the caller this
    attempt must not be marked complete -- see
    ``lambdas/push_sender/handler.py``'s docstring and
    ``docs/fcm-notification-sender.md``, section 3.
    """
    occurred_at = ring_event.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    firebase_broken = False
    unresolved_temporary_failure = False
    for installation in installations_list:
        user_id = installation.get("user_id")
        decision = decisions.get(user_id) if isinstance(user_id, str) else None
        if decision is None or decision.suppressed:
            continue
        if firebase_broken:
            counters["auth_config_failure_count"] += 1
            continue
        message = compose_message(
            token=installation["token"],
            device_id=ring_event.device_id,
            event_id=ring_event.event_id,
            event=ring_event.event,
            call_id=ring_event.call_id,
            presentation_intent=(
                None if ring_event.event == "RING_ENDED" else decision.delivery_mode
            ),
            occurred_at=occurred_at,
            now=now_utc,
        )
        try:
            result = send_with_retry(fcm, message, sleeper=sleeper)
        except FirebaseCredentialError:
            firebase_broken = True
            counters["auth_config_failure_count"] += 1
            continue
        if result.outcome == "SUCCESS":
            counters["sent_count"] += 1
        elif result.outcome == "INVALID_TOKEN":
            counters["invalid_token_count"] += 1
            deleted = cleanup.delete_invalid_installation(
                ddb,
                installations_table,
                installation_id=installation["installation_id"],
                user_id=installation["user_id"],
                token_hash=installation["token_hash"],
            )
            LOG.info(
                json.dumps(
                    {
                        "event": (
                            "push_sender_token_removed"
                            if deleted
                            else "push_sender_token_removal_skipped_race"
                        )
                    }
                )
            )
        elif result.outcome == "AUTH_OR_CONFIG_ERROR":
            # A typed AUTH_OR_CONFIG_ERROR result is the same systemic
            # failure class as a raised FirebaseCredentialError above (a
            # 401/403 from FCM means the access token itself is bad, not
            # that this one token is bad) -- so it gets exactly the same
            # treatment: stop sending anything else this invocation, and
            # let the caller decide whether to propagate as a recoverable
            # failure (see the firebase_broken check after this call).
            firebase_broken = True
            counters["auth_config_failure_count"] += 1
        elif result.outcome == "RATE_LIMITED" or result.outcome == "TEMPORARY_ERROR":
            # send_with_retry() already exhausted its local, bounded
            # retries for this one installation -- this outcome means it
            # never resolved. Never removes the token (only INVALID_TOKEN
            # does that) and never silently drops this installation from
            # the delivery: it flags the whole attempt as needing a retry.
            counters["temporary_failure_count"] += 1
            unresolved_temporary_failure = True
        else:  # PERMANENT_PAYLOAD_ERROR
            counters["permanent_failure_count"] += 1
    return firebase_broken, unresolved_temporary_failure


def _complete_temporal_suppression(
    ddb: Any,
    deliveries_table: str,
    ring_event: Any,
    attempt: int,
    counters: dict[str, int],
    reason: str | None,
    age_bucket: str | None,
    *,
    now: int,
) -> dict[str, Any]:
    if reason not in {"expired", "unknown_event_time", "future_event_time"}:
        raise ValueError("invalid temporal suppression reason")
    counter_name = {
        "expired": "expired_suppressed_count",
        "unknown_event_time": "unknown_event_time_suppressed_count",
        "future_event_time": "future_event_time_suppressed_count",
    }[reason]
    metric_name = {
        "expired": "PushSuppressedExpired",
        "unknown_event_time": "PushSuppressedUnknownEventTime",
        "future_event_time": "PushSuppressedFutureEventTime",
    }[reason]
    counters[counter_name] = 1
    outcome = f"SUPPRESSED_{reason.upper()}"
    idempotency.complete(
        ddb,
        deliveries_table,
        ring_event.device_id,
        ring_event.event_id,
        now=now,
        attempt=attempt,
        counters=counters,
        outcome=outcome,
    )
    metrics.emit(
        {metric_name: 1}, reason=reason, event_type=ring_event.event, age_bucket=age_bucket
    )
    LOG.info(
        json.dumps(
            {
                "event": "push_sender_suppressed",
                "reason": reason,
                "event_type": ring_event.event,
                "age_bucket": age_bucket,
            }
        )
    )
    return {"result": "suppressed_expired" if reason == "expired" else outcome.lower(), **counters}
