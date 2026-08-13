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
from infrastructure.stacks import ApiStack, DataStack, IoTStack, ObservabilityStack

config = get_environment_config()
env = cdk.Environment(account=config.account, region=config.region)

app = cdk.App()

data_stack = DataStack(app, stack_id(config, "Data"), config=config, env=env)
iot_stack = IoTStack(app, stack_id(config, "IoT"), config=config, env=env)
api_stack = ApiStack(app, stack_id(config, "Api"), config=config, env=env)
observability_stack = ObservabilityStack(
    app, stack_id(config, "Observability"), config=config, env=env
)

app.synth()
