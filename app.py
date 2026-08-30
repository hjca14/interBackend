#!/usr/bin/env python
"""CDK app entry point for the InterBridge backend.

Usage: ``cdk synth`` / ``cdk diff`` / ``cdk deploy`` (see README.md and
docs/deployment.md -- deploy and bootstrap are NOT authorized in this
phase).

The AWS account is taken only from ``CDK_DEFAULT_ACCOUNT`` (set by the CDK
CLI from the active credentials, or left unset in CI). The region defaults
to ``sa-east-1`` when ``CDK_DEFAULT_REGION`` is unset. Neither is hardcoded,
so this file synthesizes identically for any account/region and works in CI
without AWS credentials.
"""

from __future__ import annotations

import aws_cdk as cdk

from infrastructure.config.environment import get_environment_config
from infrastructure.config.naming import stack_id
from infrastructure.stacks import (
    ApiStack,
    DataStack,
    IngestionStack,
    IoTStack,
    NotificationStack,
    ObservabilityStack,
)

config = get_environment_config()
env = cdk.Environment(account=config.account, region=config.region)

app = cdk.App()

data_stack = DataStack(app, stack_id(config, "Data"), config=config, env=env)
iot_stack = IoTStack(app, stack_id(config, "IoT"), config=config, env=env)
api_stack = ApiStack(app, stack_id(config, "Api"), config=config, data_stack=data_stack, env=env)
api_stack.add_stack_dependency(data_stack)
notification_stack = NotificationStack(
    app, stack_id(config, "Notification"), config=config, data_stack=data_stack, env=env
)
notification_stack.add_stack_dependency(data_stack)
ingestion_stack = IngestionStack(
    app,
    stack_id(config, "Ingestion"),
    config=config,
    data_stack=data_stack,
    push_sender_function=notification_stack.function,
    env=env,
)
ingestion_stack.add_stack_dependency(data_stack)
ingestion_stack.add_stack_dependency(notification_stack)
observability_stack = ObservabilityStack(
    app,
    stack_id(config, "Observability"),
    config=config,
    ingestion_stack=ingestion_stack,
    notification_stack=notification_stack,
    env=env,
)
observability_stack.add_stack_dependency(ingestion_stack)
observability_stack.add_stack_dependency(notification_stack)

app.synth()
