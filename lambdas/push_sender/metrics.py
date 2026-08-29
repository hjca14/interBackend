"""Structured logs and CloudWatch custom metrics for the push sender.

Metrics are emitted via the CloudWatch Embedded Metric Format (EMF): a
specially-shaped JSON log line CloudWatch Logs automatically extracts into
custom metrics -- no extra API call, no extra IAM permission, no
dependency. See
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html

Deliberately never uses ``device_id``, ``user_id`` or ``event_id`` as an
EMF dimension (high cardinality); those identifiers may still appear in
the plain (non-metric) log fields for traceability, but token, credential
and raw-payload content never do.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

LOG = logging.getLogger("push_sender.metrics")
LOG.setLevel(logging.INFO)
NAMESPACE = "InterBridge/PushSender"


def emit(metrics: dict[str, int], **fields: Any) -> None:
    document: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [[]],
                    "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
                }
            ],
        },
        **metrics,
        **fields,
    }
    LOG.info(json.dumps(document, separators=(",", ":")))
