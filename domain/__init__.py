"""Framework-independent domain models for the InterBridge backend.

Everything under ``domain/`` is plain Python: no ``aws_cdk`` import, no
AWS SDK (``boto3``) call, no Lambda handler. It exists so the data shapes
and validation rules that ``infrastructure/stacks/data_stack.py`` implies
(and that a future Lambda consumer will need) are defined and tested once,
independently of both the CDK synth-time code and any not-yet-built
runtime.

See ``docs/data-model.md`` for the access patterns these models support
and ``CONTEXT.md`` ("Onboarding BLE-first") for the terminology
(``setup_code``, ``claim_session``, Fleet Provisioning temporary claim)
these models are built around.
"""
