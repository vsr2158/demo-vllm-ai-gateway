#!/usr/bin/env python3
"""
CDK app entrypoint for the lean GenAI Gateway (Sydney demo edition).

Three stacks, deployed in order:
  1. NetworkStack  — VPC, subnets, NAT, endpoints, flow logs
  2. DataStack     — RDS, Redis, S3, Secrets, KMS
  3. GatewayStack  — ECS Fargate (LiteLLM + middleware), ALB, IAM, autoscaling
"""

import os

import aws_cdk as cdk

from config import CONFIG
from stacks.network_stack import NetworkStack
from stacks.data_stack import DataStack
from stacks.gateway_stack import GatewayStack

app = cdk.App()

# Tag every taggable resource in every stack. Applied at the app level so it
# propagates to all constructs across network/data/gateway stacks.
cdk.Tags.of(app).add("auto-delete", "no")

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=CONFIG.region,
)

network = NetworkStack(app, f"{CONFIG.name}-network", cfg=CONFIG, env=env)

data = DataStack(
    app, f"{CONFIG.name}-data", cfg=CONFIG, vpc=network.vpc, env=env
)
data.add_dependency(network)

gateway = GatewayStack(
    app,
    f"{CONFIG.name}-gateway",
    cfg=CONFIG,
    vpc=network.vpc,
    db_sg=data.db_sg,
    redis_sg=data.redis_sg,
    database=data.database,
    db_secret=data.db_secret,
    master_salt_secret=data.master_salt_secret,
    salt_secret=data.salt_secret,
    provider_secret=data.provider_secret,
    redis_auth_secret=data.redis_auth_secret,
    redis_endpoint_address=data.redis_endpoint_address,
    redis_endpoint_port=data.redis_endpoint_port,
    log_bucket=data.log_bucket,
    config_bucket=data.config_bucket,
    config_object_key=data.config_object_key,
    kms_key=data.kms_key,
    env=env,
)
gateway.add_dependency(data)

app.synth()
