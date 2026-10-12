"""
Shared config for the Streamlit demo frontend.

Reads the gateway URL + API key from environment (.env supported) and defines
the verified Sydney Bedrock model catalog + per-tier groupings used by the
model-comparison and complexity-routing demos.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # load .env if present

BASE_URL = os.environ.get(
    "LITELLM_BASE_URL",
    "http://genai-gateway-alb-569044484.ap-southeast-2.elb.amazonaws.com",
).rstrip("/")

# OpenAI SDK expects the base to include /v1.
OPENAI_BASE_URL = f"{BASE_URL}/v1"

API_KEY = os.environ.get("LITELLM_API_KEY", "")

# --- Verified Sydney Bedrock chat models (model_name as served by LiteLLM) ----
# These were smoke-tested end-to-end through the gateway.
CHAT_MODELS = [
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-5-5",
    "claude-haiku-4-5",
    "nova-pro",
    "nova-lite",
    "nova-micro",
]

# Cost/capability tiers for the complexity-routing demo.
# cheap/fast for simple prompts; premium for complex reasoning.
TIER_SIMPLE = "nova-micro"        # cheapest, fastest
TIER_MODERATE = "claude-haiku-5-5"
TIER_COMPLEX = "claude-sonnet-5-5"
TIER_PREMIUM = "claude-opus-5-5"  # most capable, most expensive

# Default small set pre-selected in the comparison tab.
DEFAULT_COMPARE = ["nova-micro", "claude-haiku-5-5", "claude-sonnet-5-5"]


def missing_config() -> list[str]:
    """Return a list of human-readable config problems (empty = OK)."""
    problems = []
    if not API_KEY:
        problems.append(
            "LITELLM_API_KEY is not set. Copy .env.template to .env and set it "
            "(or export LITELLM_API_KEY)."
        )
    if not BASE_URL:
        problems.append("LITELLM_BASE_URL is not set.")
    return problems
