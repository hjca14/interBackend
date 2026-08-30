"""InterBridge backend CDK stacks.

See CONTEXT.md for the planned dependency graph between these stacks
(DataStack -> IoTStack / ApiStack -> ObservabilityStack), which is designed
to avoid circular dependencies.
"""

from infrastructure.stacks.api_stack import ApiStack
from infrastructure.stacks.data_stack import DataStack
from infrastructure.stacks.ingestion_stack import IngestionStack
from infrastructure.stacks.iot_stack import IoTStack
from infrastructure.stacks.notification_stack import NotificationStack
from infrastructure.stacks.observability_stack import ObservabilityStack

__all__ = [
    "ApiStack",
    "DataStack",
    "IngestionStack",
    "IoTStack",
    "NotificationStack",
    "ObservabilityStack",
]
