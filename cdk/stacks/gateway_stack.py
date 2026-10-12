"""
GatewayStack — ECS Fargate service (LiteLLM + middleware), ALB, IAM, autoscaling.

Replaces the Terraform `ecs` module:
  aws_ecs_cluster / aws_ecs_task_definition (2 containers) / aws_ecs_service
  aws_lb / target groups (4000, 3000) / listeners / listener rules (path routing)
  aws_security_group.{alb_sg, ecs_service_sg} + rules
  aws_iam_role.{task_role, execution_role} + policies
  app autoscaling (cpu/memory)

CloudFront + the secret-header listener rules are gated behind cfg.use_cloudfront
(off by default for the demo = direct ALB).
"""

from aws_cdk import (
    Stack,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    aws_s3 as s3,
    aws_ecr_assets as ecr_assets,
    aws_kms as kms,
    CfnOutput,
    SecretValue,
)
from constructs import Construct

from config import GatewayConfig

# ALB path routes that must be served by the middleware container (:3000).
# Mirrors the TF listener rules forwarding to tg_3000.
MIDDLEWARE_PATHS = [
    "/bedrock/model/*",
    "/v1/chat/completions",
    "/chat/completions",
    "/chat-history",
    "/bedrock/chat-history",
    "/bedrock/health/liveliness",
    "/session-ids",
    "/key/generate",
    "/user/new",
]


class GatewayStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        cfg: GatewayConfig,
        vpc: ec2.IVpc,
        db_sg: ec2.ISecurityGroup,
        redis_sg: ec2.ISecurityGroup,
        database: rds.IDatabaseInstance,
        db_secret: secretsmanager.ISecret,
        master_salt_secret: secretsmanager.ISecret,
        salt_secret: secretsmanager.ISecret,
        provider_secret: secretsmanager.ISecret,
        redis_auth_secret: secretsmanager.ISecret,
        redis_endpoint_address: str,
        redis_endpoint_port: str,
        log_bucket: s3.IBucket,
        config_bucket: s3.IBucket,
        config_object_key: str,
        kms_key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        # ===================================================================
        # Security groups
        # ===================================================================
        alb_sg = ec2.SecurityGroup(
            self, "AlbSg", vpc=vpc, description="ALB SG",
            allow_all_outbound=True,
        )
        # Public demo: ALB open on 80/443. (CloudFront toggle would narrow this.)
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS")

        ecs_sg = ec2.SecurityGroup(
            self, "EcsServiceSg", vpc=vpc, description="ECS service SG",
            allow_all_outbound=True,
        )
        # ALB -> ECS on both container ports.
        ecs_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(4000), "ALB to LiteLLM")
        ecs_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(3000), "ALB to middleware")
        # ECS -> RDS (5432) and Redis (6379).
        # Add these as standalone ingress resources in THIS stack referencing the
        # data stack's SG ids. Calling db_sg.add_ingress_rule(ecs_sg, ...) would
        # make the data stack depend on the gateway stack (cyclic), so we attach
        # the rule to the ECS SG's egress side is not enough — instead create the
        # ingress rule resource here bound to the data SG id.
        ec2.CfnSecurityGroupIngress(
            self,
            "DbIngressFromEcs",
            group_id=db_sg.security_group_id,
            ip_protocol="tcp",
            from_port=5432,
            to_port=5432,
            source_security_group_id=ecs_sg.security_group_id,
            description="ECS to Postgres",
        )
        ec2.CfnSecurityGroupIngress(
            self,
            "RedisIngressFromEcs",
            group_id=redis_sg.security_group_id,
            ip_protocol="tcp",
            from_port=6379,
            to_port=6379,
            source_security_group_id=ecs_sg.security_group_id,
            description="ECS to Redis",
        )

        # ===================================================================
        # IAM roles (task + execution)
        # ===================================================================
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"{cfg.name}-ecs-execution-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"{cfg.name}-ecs-task-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        # Bedrock + SageMaker invoke (mirrors TF task_role_policy_doc).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockAccess",
                actions=["bedrock:*"],
                resources=["*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SageMakerInvoke",
                actions=["sagemaker:InvokeEndpoint"],
                resources=["*"],
            )
        )
        # Grant S3 log-bucket access via an identity-based policy on the task
        # role (not bucket.grant_* which would edit the data stack's bucket
        # policy and create a cross-stack cycle).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3LogBucketAccess",
                actions=["s3:*"],
                resources=[log_bucket.bucket_arn, f"{log_bucket.bucket_arn}/*"],
            )
        )
        # Read access to the config bucket so LiteLLM can load config.yaml
        # at startup (identity-based, same cycle-avoidance rationale).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3ConfigBucketAccess",
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[config_bucket.bucket_arn, f"{config_bucket.bucket_arn}/*"],
            )
        )

        # Secrets the containers read at startup (execution role injects them).
        for secret in (master_salt_secret, salt_secret, provider_secret,
                       db_secret, redis_auth_secret):
            secret.grant_read(execution_role)
            secret.grant_read(task_role)

        # ===================================================================
        # ECS cluster + task definition (2 containers)
        # ===================================================================
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc,
            cluster_name=f"{cfg.name}-cluster",
            container_insights=True,
        )

        # CPU architecture (Graviton/ARM64 or X86_64) from config.
        cpu_arch = (
            ecs.CpuArchitecture.ARM64 if cfg.architecture == "arm"
            else ecs.CpuArchitecture.X86_64
        )

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family=f"{cfg.name}-fargate-task",
            cpu=cfg.vcpus * 1024,
            memory_limit_mib=cfg.vcpus * 1024 * 2,
            execution_role=execution_role,
            task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=cpu_arch,
            ),
        )

        # DB connection URL assembled from the RDS-generated secret.
        #
        # IMPORTANT: the RDS-generated secret (db_secret) stores a JSON blob
        # ({"username":...,"password":...,"host":...,...}). LiteLLM/Prisma and
        # the middleware's SQLAlchemy both expect a *connection URI string*
        # (postgresql://user:pass@host:port/db), NOT the raw JSON. Injecting
        # db_secret directly made both containers crash-loop with:
        #   Prisma  P1012: the URL must start with the protocol postgresql://
        #   SQLAlchemy: Could not parse SQLAlchemy URL from given URL string
        #
        # So we materialise a dedicated secret holding the assembled URI and
        # inject *that* into the containers (mirrors the Terraform
        # db_url_secret). Each piece uses CDK dynamic references so the final
        # stored value resolves to a real postgresql:// string at deploy time.
        db_url_value = (
            "postgresql://"
            f"{db_secret.secret_value_from_json('username').unsafe_unwrap()}:"
            f"{db_secret.secret_value_from_json('password').unsafe_unwrap()}@"
            f"{database.db_instance_endpoint_address}:"
            f"{database.db_instance_endpoint_port}/litellm"
        )
        db_url_secret = secretsmanager.Secret(
            self,
            "DbUrlSecret",
            secret_name=f"{cfg.name}-DBUrl",
            secret_string_value=SecretValue.unsafe_plain_text(db_url_value),
        )
        db_url_secret.grant_read(execution_role)
        db_url_secret.grant_read(task_role)

        # --- LiteLLM container (:4000) -------------------------------------
        litellm_container = task_def.add_container(
            "LiteLLMContainer",
            image=ecs.ContainerImage.from_registry(
                f"{cfg.litellm_image}:{cfg.litellm_version}"
            ),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="LiteLLM",
                log_group=logs.LogGroup(
                    self, "LiteLLMLogGroup",
                    log_group_name=f"/ecs/{cfg.name}-litellm",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=self._removal(),
                ),
            ),
            environment={
                "LITELLM_LOG": "DEBUG",
                "LITELLM_CONFIG_BUCKET_NAME": config_bucket.bucket_name,
                "LITELLM_CONFIG_BUCKET_OBJECT_KEY": config_object_key,
                "UI_USERNAME": cfg.ui_username,
                "REDIS_HOST": redis_endpoint_address,
                "REDIS_PORT": redis_endpoint_port,
                "REDIS_SSL": "True",
                "NO_DOCS": str(cfg.disable_swagger_page),
                "DISABLE_ADMIN_UI": str(cfg.disable_admin_ui),
            },
            secrets={
                "DATABASE_URL": ecs.Secret.from_secrets_manager(db_url_secret),
                "LITELLM_MASTER_KEY": ecs.Secret.from_secrets_manager(
                    master_salt_secret, "LITELLM_MASTER_KEY"
                ),
                "UI_PASSWORD": ecs.Secret.from_secrets_manager(
                    master_salt_secret, "LITELLM_MASTER_KEY"
                ),
                "LITELLM_SALT_KEY": ecs.Secret.from_secrets_manager(
                    salt_secret, "LITELLM_SALT_KEY"
                ),
                "REDIS_PASSWORD": ecs.Secret.from_secrets_manager(redis_auth_secret),
            },
        )
        litellm_container.add_port_mappings(
            ecs.PortMapping(container_port=4000, protocol=ecs.Protocol.TCP)
        )

        # --- Middleware container (:3000), built from ../middleware --------
        middleware_platform = (
            ecr_assets.Platform.LINUX_ARM64 if cfg.architecture == "arm"
            else ecr_assets.Platform.LINUX_AMD64
        )
        middleware_image = ecr_assets.DockerImageAsset(
            self, "MiddlewareImage",
            directory="../middleware",
            platform=middleware_platform,
        )
        middleware_container = task_def.add_container(
            "MiddlewareContainer",
            image=ecs.ContainerImage.from_docker_image_asset(middleware_image),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="Middleware",
                log_group=logs.LogGroup(
                    self, "MiddlewareLogGroup",
                    log_group_name=f"/ecs/{cfg.name}-middleware",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=self._removal(),
                ),
            ),
            environment={
                "OKTA_ISSUER": cfg.okta_issuer,
                "OKTA_AUDIENCE": cfg.okta_audience,
            },
            secrets={
                "DATABASE_MIDDLEWARE_URL": ecs.Secret.from_secrets_manager(db_url_secret),
                "MASTER_KEY": ecs.Secret.from_secrets_manager(
                    master_salt_secret, "LITELLM_MASTER_KEY"
                ),
            },
        )
        middleware_container.add_port_mappings(
            ecs.PortMapping(container_port=3000, protocol=ecs.Protocol.TCP)
        )

        # ===================================================================
        # ECS Fargate service
        # ===================================================================
        service = ecs.FargateService(
            self,
            "Service",
            service_name="LiteLLMService",
            cluster=cluster,
            task_definition=task_def,
            desired_count=cfg.desired_capacity,
            security_groups=[ecs_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            health_check_grace_period=Duration.seconds(300),
            assign_public_ip=False,
        )

        # ===================================================================
        # ALB + target groups + listener + path routing
        # ===================================================================
        subnet_type = (
            ec2.SubnetType.PUBLIC if cfg.public_load_balancer
            else ec2.SubnetType.PRIVATE_WITH_EGRESS
        )
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            load_balancer_name=f"{cfg.name}-alb",
            vpc=vpc,
            internet_facing=cfg.public_load_balancer,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=subnet_type),
        )
        # (ALB access logging intentionally omitted in the demo to avoid a
        # cross-stack bucket-policy cycle; LiteLLM request logs still go to S3
        # via the task role.)

        # Target group for LiteLLM (:4000) — the default.
        tg_4000 = elbv2.ApplicationTargetGroup(
            self,
            "Tg4000",
            vpc=vpc,
            port=4000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[service.load_balancer_target(
                container_name="LiteLLMContainer", container_port=4000
            )],
            health_check=elbv2.HealthCheck(
                path="/health/liveliness",
                port="4000",
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                timeout=Duration.seconds(10),
                interval=Duration.seconds(30),
            ),
        )

        # Target group for middleware (:3000).
        tg_3000 = elbv2.ApplicationTargetGroup(
            self,
            "Tg3000",
            vpc=vpc,
            port=3000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[service.load_balancer_target(
                container_name="MiddlewareContainer", container_port=3000
            )],
            health_check=elbv2.HealthCheck(
                path="/bedrock/health/liveliness",
                port="3000",
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
                timeout=Duration.seconds(10),
                interval=Duration.seconds(30),
            ),
        )

        # HTTP listener (demo: direct ALB, no TLS termination here).
        # Default action forwards to LiteLLM; middleware paths overridden below.
        listener = alb.add_listener(
            "HttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_target_groups=[tg_4000],
        )

        # Path-based routing to the middleware target group.
        # ALB allows at most 5 path-pattern values per rule, so chunk the
        # middleware paths across multiple rules with distinct priorities.
        for i, chunk in enumerate(_chunk(MIDDLEWARE_PATHS, 5)):
            listener.add_action(
                f"MiddlewareRoutes{i}",
                priority=10 + i,
                conditions=[elbv2.ListenerCondition.path_patterns(chunk)],
                action=elbv2.ListenerAction.forward([tg_3000]),
            )

        # ===================================================================
        # Autoscaling (CPU + memory target tracking)
        # ===================================================================
        scaling = service.auto_scale_task_count(
            min_capacity=cfg.min_capacity,
            max_capacity=cfg.max_capacity,
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=cfg.cpu_target_utilization_percent,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )
        scaling.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=cfg.memory_target_utilization_percent,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )

        # ===================================================================
        # Outputs
        # ===================================================================
        CfnOutput(self, "ServiceURL", value=f"http://{alb.load_balancer_dns_name}")
        CfnOutput(self, "AdminUIURL", value=f"http://{alb.load_balancer_dns_name}/ui")
        CfnOutput(
            self, "MasterKeySecretArn", value=master_salt_secret.secret_arn,
            description="Secrets Manager ARN holding LITELLM_MASTER_KEY",
        )

    def _removal(self):
        from aws_cdk import RemovalPolicy
        return RemovalPolicy.DESTROY  # demo


def _chunk(items, size):
    """Yield successive `size`-length chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]
