"""
NetworkStack — VPC, subnets, NAT, flow logs, and VPC endpoints.

Replaces the networking portion of the Terraform `base` module:
  aws_vpc / aws_subnet.* / aws_internet_gateway / aws_nat_gateway / aws_eip
  / aws_route_table.* / aws_flow_log + log group + role
  / aws_vpc_endpoint.* / vpc_endpoints_sg

CDK's ec2.Vpc collapses the VPC + subnets + IGW + NAT + route tables + EIP
into a single construct. We add the interface/gateway endpoints the gateway
actually needs (Bedrock, ECR, Secrets Manager, CloudWatch Logs, S3).
"""

from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
)
from constructs import Construct

from config import GatewayConfig


class NetworkStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, cfg: GatewayConfig, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg

        # --- VPC: 2 AZs, public + private-with-NAT subnets -----------------
        # Mirrors TF: 10.0.0.0/16, public subnets map_public_ip, private via NAT.
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=f"{cfg.name}-vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=cfg.max_azs,
            nat_gateways=1,  # single NAT GW for demo (TF nat_gateway_count=1)
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        # --- VPC Flow Logs (ALL traffic) to CloudWatch ---------------------
        self.vpc.add_flow_log(
            "FlowLog",
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # --- Security group for interface endpoints ------------------------
        self.endpoints_sg = ec2.SecurityGroup(
            self,
            "VpcEndpointsSg",
            vpc=self.vpc,
            description="SG for VPC interface endpoints",
            allow_all_outbound=True,
        )
        # Allow HTTPS from within the VPC to the interface endpoints.
        self.endpoints_sg.add_ingress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS from VPC to interface endpoints",
        )

        # --- Interface endpoints (private access to AWS APIs) --------------
        # Subset the gateway actually needs; keeps the demo lean vs TF's 18.
        interface_endpoints = {
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "Bedrock": ec2.InterfaceVpcEndpointAwsService.BEDROCK,
            "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            "CloudWatchLogs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "Sts": ec2.InterfaceVpcEndpointAwsService.STS,
        }
        for name, svc in interface_endpoints.items():
            self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=svc,
                security_groups=[self.endpoints_sg],
                private_dns_enabled=True,
            )

        # --- S3 gateway endpoint (for config/log buckets + ECR layers) -----
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )
