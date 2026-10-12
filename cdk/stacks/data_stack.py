"""
DataStack — RDS Postgres, ElastiCache Redis, S3 log bucket, and Secrets.

Replaces the data portion of the Terraform `base` module:
  aws_db_instance / aws_db_subnet_group / aws_db_parameter_group / db_sg
  aws_elasticache_replication_group / subnet_group / parameter_group / redis_sg
  aws_secretsmanager_secret.{litellm_master_salt, db_secret_main, db_url_secret,
    litellm_other_secrets}
  S3 access/log bucket + KMS

Security groups here are created with ingress left open to be wired by the
GatewayStack (which owns the ECS service SG). We expose the SGs and secrets
as attributes for the gateway stack to consume.
"""

import json

from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_elasticache as elasticache,
    aws_secretsmanager as secretsmanager,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_kms as kms,
)
from constructs import Construct

from config import GatewayConfig


class DataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: GatewayConfig,
        vpc: ec2.IVpc,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        self.vpc = vpc

        # --- KMS key for data-at-rest encryption ---------------------------
        self.kms_key = kms.Key(
            self,
            "DataKey",
            description=f"{cfg.name} data encryption key",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,  # demo
        )

        # ===================================================================
        # Security groups
        # ===================================================================
        self.db_sg = ec2.SecurityGroup(
            self, "DbSg", vpc=vpc, description="RDS Postgres SG",
            allow_all_outbound=True,
        )
        self.redis_sg = ec2.SecurityGroup(
            self, "RedisSg", vpc=vpc, description="ElastiCache Redis SG",
            allow_all_outbound=True,
        )

        # ===================================================================
        # Secrets Manager
        # ===================================================================
        # (1) LiteLLM master + salt keys (sk- prefixed). TF generated two random
        #     passwords and prefixed "sk-". We generate a JSON secret with both.
        self.master_salt_secret = secretsmanager.Secret(
            self,
            "LiteLLMMasterSalt",
            secret_name=f"{cfg.name}-LiteLLMMasterSalt",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({}),
                generate_string_key="LITELLM_MASTER_KEY",
                exclude_punctuation=True,
                include_space=False,
                password_length=24,
            ),
        )
        # Note: a single generated field is produced by SecretStringGenerator.
        # LiteLLM needs both MASTER and SALT keys; we add SALT via a second
        # secret to keep each independently generated.
        self.salt_secret = secretsmanager.Secret(
            self,
            "LiteLLMSalt",
            secret_name=f"{cfg.name}-LiteLLMSalt",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({}),
                generate_string_key="LITELLM_SALT_KEY",
                exclude_punctuation=True,
                include_space=False,
                password_length=24,
            ),
        )

        # (2) External provider API keys (OpenAI, Anthropic, etc.).
        self.provider_secret = secretsmanager.Secret(
            self,
            "LiteLLMProviderKeys",
            secret_name=f"{cfg.name}-LiteLLMProviderKeys",
            secret_object_value={
                # Stored as a JSON secret; placeholders by default.
                k: _plain(v) for k, v in cfg.provider_api_keys.items()
            },
        )

        # ===================================================================
        # RDS Postgres 15
        # ===================================================================
        db_subnet_group = rds.SubnetGroup(
            self,
            "DbSubnetGroup",
            description=f"{cfg.name} db subnet group",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
        )

        db_param_group = rds.ParameterGroup(
            self,
            "DbParamGroup",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_15
            ),
            parameters={
                "log_statement": "all",
                "log_min_duration_statement": "1",
            },
        )

        # RDS generates and manages the master credentials in its own secret
        # (username llmproxy). This replaces TF's db_secret_main.
        self.db_credentials = rds.Credentials.from_generated_secret(
            username="llmproxy",
            secret_name=f"{cfg.name}-DBSecret",
        )

        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=f"{cfg.name}-litellm-db",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_15
            ),
            instance_type=ec2.InstanceType(cfg.rds_instance_class.replace("db.", "")),
            vpc=vpc,
            subnet_group=db_subnet_group,
            security_groups=[self.db_sg],
            credentials=self.db_credentials,
            allocated_storage=cfg.rds_allocated_storage_gb,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            storage_encryption_key=self.kms_key,
            database_name="litellm",
            multi_az=cfg.rds_multi_az,
            parameter_group=db_param_group,
            cloudwatch_logs_exports=["postgresql"],
            enable_performance_insights=True,
            monitoring_interval=Duration.seconds(60),
            auto_minor_version_upgrade=True,
            deletion_protection=False,  # demo
            removal_policy=RemovalPolicy.DESTROY,  # demo
        )
        self.db_secret = self.database.secret  # generated credentials secret

        # ===================================================================
        # ElastiCache Redis 7.1 (TLS + auth token)
        # ===================================================================
        redis_subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description=f"{cfg.name} redis subnet group",
            subnet_ids=vpc.select_subnets(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ).subnet_ids,
            cache_subnet_group_name=f"{cfg.name}-redis-subnet-group",
        )

        redis_param_group = elasticache.CfnParameterGroup(
            self,
            "RedisParamGroup",
            cache_parameter_group_family="redis7",
            description="Redis parameter group",
            properties={"timeout": "0"},
        )

        # Redis AUTH token stored in its own secret (TF used random_password).
        self.redis_auth_secret = secretsmanager.Secret(
            self,
            "RedisAuthToken",
            secret_name=f"{cfg.name}-RedisAuthToken",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                include_space=False,
                password_length=32,
            ),
        )

        self.redis = elasticache.CfnReplicationGroup(
            self,
            "Redis",
            replication_group_id=f"{cfg.name}-redis",
            replication_group_description="redis",
            engine="redis",
            engine_version="7.1",
            cache_node_type=cfg.redis_node_type,
            num_cache_clusters=cfg.redis_num_cache_clusters,
            automatic_failover_enabled=cfg.redis_num_cache_clusters > 1,
            multi_az_enabled=cfg.redis_num_cache_clusters > 1,
            cache_parameter_group_name=redis_param_group.ref,
            cache_subnet_group_name=redis_subnet_group.ref,
            security_group_ids=[self.redis_sg.security_group_id],
            port=6379,
            at_rest_encryption_enabled=True,
            transit_encryption_enabled=True,
            auth_token=self.redis_auth_secret.secret_value.unsafe_unwrap(),
        )
        self.redis.add_dependency(redis_subnet_group)
        self.redis.add_dependency(redis_param_group)

        # ===================================================================
        # S3 log / access-log bucket (SSL-only, encrypted, private)
        # ===================================================================
        self.log_bucket = s3.Bucket(
            self,
            "LogBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # demo
            auto_delete_objects=True,  # demo
        )

        # ===================================================================
        # S3 config bucket — holds the merged LiteLLM config.yaml that the
        # LiteLLM container loads at startup (LITELLM_CONFIG_BUCKET_*).
        # Build the artifact first with: python build_config.py
        # ===================================================================
        self.config_bucket = s3.Bucket(
            self,
            "ConfigBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,  # demo
            auto_delete_objects=True,  # demo
        )

        self.config_object_key = "config.yaml"
        s3deploy.BucketDeployment(
            self,
            "ConfigDeployment",
            sources=[s3deploy.Source.asset("./assets")],
            destination_bucket=self.config_bucket,
            # only ship config.yaml from assets/
            exclude=["*"],
            include=["config.yaml"],
            prune=False,
        )

    # Convenience accessors -------------------------------------------------
    @property
    def redis_endpoint_address(self) -> str:
        return self.redis.attr_primary_end_point_address

    @property
    def redis_endpoint_port(self) -> str:
        return self.redis.attr_primary_end_point_port


def _plain(value: str):
    """Wrap a plain string as a SecretValue for secret_object_value."""
    from aws_cdk import SecretValue
    return SecretValue.unsafe_plain_text(value)
