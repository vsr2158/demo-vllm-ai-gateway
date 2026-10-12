"""
Central configuration for the lean GenAI Gateway CDK app (Sydney demo edition).

Everything that used to live in `.env` / Terraform `variables.tf` is consolidated
here so a demo can be configured in one place. Values are intentionally modest
(small instance classes, 2 AZs) to keep demo cost and spin-up time low.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayConfig:
    # --- Deployment target -------------------------------------------------
    region: str = "ap-southeast-2"          # Sydney
    name: str = "genai-gateway"             # resource name prefix
    max_azs: int = 2                        # demo: 2 AZs is enough

    # --- Container images --------------------------------------------------
    # LiteLLM upstream image tag (matches .env.template LITELLM_VERSION).
    # v1.105.0 is the current "Latest" stable release (resolves to the same
    # digest as main-stable). Multi-arch, so ARM64/Graviton Fargate is fine.
    litellm_version: str = "v1.105.0"
    litellm_image: str = "ghcr.io/berriai/litellm"
    # Middleware is built from ../middleware via a DockerImageAsset.

    # --- Compute (Fargate) -------------------------------------------------
    vcpus: int = 2                          # task cpu = vcpus*1024, mem = vcpus*2048
    architecture: str = "arm"               # "arm" (Graviton, cheaper) or "x86"
    desired_capacity: int = 1               # demo: 1 task (upstream default 2)
    min_capacity: int = 1
    max_capacity: int = 2
    cpu_target_utilization_percent: int = 60
    memory_target_utilization_percent: int = 60

    # --- Data layer --------------------------------------------------------
    rds_instance_class: str = "db.t3.small"
    rds_allocated_storage_gb: int = 20
    rds_multi_az: bool = False              # demo: single-AZ to save cost/time
    redis_node_type: str = "cache.t3.micro"
    redis_num_cache_clusters: int = 1       # demo: single node (no failover)

    # --- Edge / auth toggles ----------------------------------------------
    public_load_balancer: bool = True       # ALB in public subnets
    use_cloudfront: bool = False            # demo: direct ALB; toggle on to harden
    disable_swagger_page: bool = False
    disable_admin_ui: bool = False
    ui_username: str = "admin"

    # Optional Okta JWT (middleware). Empty = disabled.
    okta_issuer: str = ""
    okta_audience: str = "api://default"

    # External provider API keys (placeholders; set real values to use them).
    # Only Bedrock is used by default and needs no key (uses the task IAM role).
    provider_api_keys: dict = field(default_factory=lambda: {
        "OPENAI_API_KEY": "placeholder",
        "AZURE_OPENAI_API_KEY": "placeholder",
        "AZURE_API_KEY": "placeholder",
        "ANTHROPIC_API_KEY": "placeholder",
        "GROQ_API_KEY": "placeholder",
        "COHERE_API_KEY": "placeholder",
        "CO_API_KEY": "placeholder",
        "HF_TOKEN": "placeholder",
        "HUGGINGFACE_API_KEY": "placeholder",
        "DATABRICKS_API_KEY": "placeholder",
        "GEMINI_API_KEY": "placeholder",
        "CODESTRAL_API_KEY": "placeholder",
        "MISTRAL_API_KEY": "placeholder",
        "AZURE_AI_API_KEY": "placeholder",
        "NVIDIA_NIM_API_KEY": "placeholder",
        "XAI_API_KEY": "placeholder",
        "PERPLEXITYAI_API_KEY": "placeholder",
        "GITHUB_API_KEY": "placeholder",
        "DEEPSEEK_API_KEY": "placeholder",
        "AI21_API_KEY": "placeholder",
        "LANGSMITH_API_KEY": "placeholder",
        "LANGFUSE_SECRET_KEY": "placeholder",
    })


CONFIG = GatewayConfig()
