# Multi-Provider GenAI Gateway + Demo (AWS CDK, Sydney)

A **dual-purpose** repository:

1. **AI Gateway (core capability)** — deploy a production-style, multi-provider
   generative-AI gateway to AWS with **AWS CDK (Python)**. Built on
   [LiteLLM](https://github.com/BerriAI/litellm) + a Bedrock-native FastAPI
   middleware, it gives you one OpenAI-compatible endpoint in front of Amazon
   Bedrock (and other providers), with virtual API keys, budgets, rate limits,
   caching, routing, and guardrails.

2. **Demo layer (optional, additive)** — a lightweight **Streamlit** app that
   runs *on top of* a deployed gateway to showcase its capabilities (chatbot,
   model comparison, complexity-based routing, and more). The demo is **not
   required** to run the gateway; it is extra functionality that layers on after
   the gateway is up and needs some **post-deploy configuration** on the
   gateway / LiteLLM side.

> You can deploy **Part 1 alone** as a reusable capability. **Part 2** is for
> demonstrating that capability and is documented separately in
> **[`README-DEMO.md`](README-DEMO.md)**.

This is a lean, demo-focused adaptation of the AWS Solutions Library guidance
*Multi-Provider Generative AI Gateway on AWS*: **CDK instead of Terraform**,
**ECS Fargate only** (EKS dropped), single region (**`ap-southeast-2`**), direct
ALB by default with a CloudFront toggle.

---

## Table of contents

- [Part 1 — AI Gateway capability](#part-1--ai-gateway-capability)
  - [Architecture](#architecture)
  - [CDK stacks](#cdk-stacks)
  - [AWS services](#aws-services)
  - [Prerequisites](#prerequisites)
  - [Deploy](#deploy)
  - [Models (Sydney)](#models-sydney)
  - [Security](#security)
  - [Cost](#cost)
- [Part 2 — Demo layer](#part-2--demo-layer)
  - [Gateway feature matrix](#gateway-feature-matrix)
  - [Post-Phase-1 configuration the demo needs](#post-phase-1-configuration-the-demo-needs)
- [Teardown](#teardown)
- [Attribution](#attribution)

---

# Part 1 — AI Gateway capability

## Architecture

```
        Client (SDK / curl / Streamlit demo)
                     │  Authorization: Bearer sk-...
                     ▼
          Application Load Balancer   (optional CloudFront + WAF)
                     │  path routing
     ┌───────────────┴────────────────┐
     ▼                                 ▼
 LiteLLM proxy  :4000          Bedrock middleware :3000
 (virtual keys, routing,       (Bedrock-native interface,
  budgets, caching, guardrails) chat history, Okta JWT opt.)
     │                                 │
     ▼                                 ▼
 RDS Postgres   ElastiCache Redis   Amazon Bedrock (Claude, Nova, …)
 (keys/usage)   (cache/settings)    Secrets Manager · S3 · KMS
```

- Clients hit **one OpenAI-compatible endpoint**; LiteLLM fans out to Bedrock
  (and other providers) behind a unified API.
- The **middleware** adds a Bedrock-native interface, chat history, and optional
  Okta JWT auth, and serves specific paths (`/bedrock/*`, `/chat/completions`,
  key/user management) via ALB path rules.
- State lives in **RDS** (keys/users/teams/budgets/spend + chat history),
  **Redis** (cache), **S3** (config + logs), **Secrets Manager** (keys/creds).
  Containers are **stateless**.

## CDK stacks

```
cdk/
  app.py                      # wires the 3 stacks
  config.py                   # region, sizing, flags (was .env + variables.tf)
  build_config.py             # merges base + region model lists -> assets/config.yaml
  stacks/
    network_stack.py          # genai-gateway-network : VPC, subnets, NAT, VPC endpoints, flow logs
    data_stack.py             # genai-gateway-data    : RDS, Redis, S3 (logs+config), Secrets, KMS
    gateway_stack.py          # genai-gateway-gateway : ECR image, ECS Fargate, ALB + rules, (opt) CloudFront
```

## AWS services

| Service | Role |
|---|---|
| Amazon Bedrock | Foundation models (Claude, Nova, Titan, Cohere, …) |
| Amazon ECS (Fargate) | Runs LiteLLM + middleware containers (ARM64/Graviton) |
| Amazon ECR | Stores the built middleware image |
| Amazon VPC | Network isolation (public/private subnets, NAT, endpoints) |
| Application Load Balancer | Path-routed entry to the two containers |
| Amazon RDS (Postgres) | Virtual keys, users, teams, budgets, spend, chat history |
| Amazon ElastiCache (Redis) | Response/prompt caching + settings |
| Amazon S3 | LiteLLM config bucket + access/log bucket |
| AWS Secrets Manager | Master/salt keys, DB URL, Redis auth, provider keys |
| AWS KMS | Encryption keys for data at rest |
| Amazon CloudWatch | Container logs + metrics |
| AWS WAF / CloudFront | Optional edge protection (toggle) |
| AWS IAM | Task + execution roles (least privilege) |

## Prerequisites

| Tool | Purpose |
|---|---|
| AWS CDK v2 | Deploy the stacks |
| Node.js | CDK runtime |
| Python 3.11+ | CDK app + middleware + demo |
| Docker | Build the middleware image |
| AWS credentials | For `ap-southeast-2`, with Bedrock model access enabled |

> Terraform is **not** used in this edition.

## Deploy

```bash
cd cdk
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Regenerate the merged model config (base + Sydney Bedrock) -> assets/config.yaml
python build_config.py ap-southeast-2

# Bootstrap once per account/region
cdk bootstrap aws://<account-id>/ap-southeast-2

# Deploy base stacks first, then the gateway
cdk deploy genai-gateway-network genai-gateway-data
cdk deploy genai-gateway-gateway
```

Outputs: **ServiceURL**, **AdminUIURL**, **MasterKeySecretArn** (the master key
lives in Secrets Manager, never in source).

Smoke test:
```bash
curl $SERVICE_URL/health/liveliness            # LiteLLM   -> "I'm alive!"
curl $SERVICE_URL/bedrock/health/liveliness    # middleware -> {"status":"healthy",...}
```

## Models (Sydney)

Bedrock models are defined in `config/default-config-ap-southeast-2.yaml`;
non-Bedrock providers live in `config/default-config-base.yaml`. The region file
lists **only models verified active + invokable in-account**, preferring
`au.*` (Australia-local) then `apac.*` inference profiles:

- **Claude** (`au.*`): sonnet-4-5 / 4-6 / 5 / 5-5, haiku-4-5 / 5-5,
  opus-4-6 / 4-7 / 4-8 / 5 / 5-5
- **Amazon Nova** (`apac.*`): micro, lite, pro
- **Embeddings:** Titan v2 (text + image), Cohere embed english / multilingual

> Enable access to the required models in the Bedrock console for the
> account/region before deploying. Legacy direct `anthropic.claude-3.x` ids are
> intentionally excluded (Bedrock marks them legacy / inference-profile-only).

## Security

- Client access requires a **LiteLLM virtual key** (`sk-...`) or the master key.
- Master/salt keys, DB connection URL, Redis auth, and provider keys are
  generated at deploy time and stored in **Secrets Manager** — never in source.
- The Fargate **task role** grants Bedrock / Secrets / S3 access — no static AWS
  credentials in containers.
- Direct-ALB demo mode is public but useless without a valid key; enable the
  **CloudFront toggle** (secret-header auth + Shield) or **WAF** to harden.
- Optional **Okta OAuth2 JWT** auth via the middleware (off by default).

## Cost

Baseline demo footprint (single region, 1 Fargate task, single-AZ RDS, 1-node
Redis) is modest and idles cheaply; the dominant variable cost is **Bedrock
model usage**. Use per-key **budgets** (see Part 2) to cap spend. Tear down with
`cdk destroy --all` when done. For current prices, see each service's AWS
pricing page.

---

# Part 2 — Demo layer

The demo is a local **Streamlit** app (`frontend/`) that calls the deployed
gateway's OpenAI-compatible endpoint to showcase gateway capabilities. It is
**optional** and **additive** — the gateway (Part 1) runs fine without it.

**Running the demo and its full setup are documented in
[`README-DEMO.md`](README-DEMO.md).** This section documents *which* gateway
features the demo showcases and *what configuration* each needs after Part 1.

## Gateway feature matrix

| Feature | What it does | Status | Demo surface |
|---|---|---|---|
| **Unified multi-provider API** | One OpenAI-compatible endpoint across Bedrock + others | ✅ Enabled | Chatbot, comparison |
| **Intelligent routing** | Usage/latency load-balancing; cost-based model selection | ✅ Enabled | Complexity-routing tab |
| **Caching** | Redis response caching — identical requests return instantly, $0 | ✅ Enabled | (planned tab) |
| **Budgets & rate limits** | Per-key / per-user `$` budgets, `rpm`/`tpm` caps | ✅ Enabled | (planned tab) |
| **Virtual keys** | Scoped `sk-...` keys (models, budget, limits) | ✅ Enabled | Sidebar / Admin UI |
| **Guardrails** | Bedrock Guardrails: block topics, mask PII, filter — across all providers | ⚙️ Needs config | (planned tab) |
| **Fallbacks / resiliency** | Auto-failover to another model on error / context overflow | ⚙️ Needs config | (planned tab) |
| **Spend tracking** | Per-key/model/user spend from LiteLLM → RDS | ⚙️ Needs config | (planned tab) |
| **Semantic caching** | Cache *similar* (not just identical) prompts | ⚙️ Needs config | (planned) |
| **MCP (Model Context Protocol)** | Expose/proxy MCP tool servers through the gateway | 🔜 Placeholder (future) | TBD |
| **Agents** | Multi-step agentic / tool-use workflows on the gateway | 🔜 Placeholder (future) | TBD |

Legend: ✅ enabled in the deployed gateway · ⚙️ requires post-deploy config
· 🔜 planned, not yet built.

## Post-Phase-1 configuration the demo needs

After the gateway is deployed (Part 1), the demo requires some gateway/LiteLLM
configuration. These are tracked here and detailed in
[`README-DEMO.md`](README-DEMO.md) as each is built out.

- **Connection** — the Streamlit app needs the **ServiceURL** and an API key
  (master key, or a scoped virtual key created in the Admin UI). *(Ready.)*
- **Virtual keys for the demo** — scoped keys (models + budget) created via the
  Admin UI or `/key/generate`. *(Ready.)*
- **Caching demo** — caching is already enabled; the demo needs to surface
  cache-hit metadata. *(Config: none; UI work pending.)*
- **Budgets / rate-limit demo** — create a low-budget key to show enforcement.
  *(Config: key creation; UI work pending.)*
- **Guardrails demo** — create a **Bedrock Guardrail** (block topics / PII mask)
  and reference its `guardrailIdentifier` in `config/default-config-base.yaml`,
  then redeploy the data stack + restart the service. *(Config: TBD — details
  to be added as we build this.)*
- **Fallbacks demo** — uncomment/define `fallbacks` in `router_settings` and
  redeploy config. *(Config: TBD.)*
- **MCP / Agents** — 🔜 placeholder; configuration to be defined later.

> **Note:** additional demo configuration will be added here as each feature is
> built. This list is the single source of truth for "what must be set on the
> gateway for the demo to work."

---

## Teardown

```bash
cd cdk && . .venv/bin/activate
cdk destroy --all
```

Removes all three stacks. RDS/Redis/buckets use demo-friendly removal policies
for clean teardown.

## Attribution

Adapted from the AWS Solutions Library guidance *Multi-Provider Generative AI
Gateway on AWS* (MIT-0). Built on [LiteLLM](https://github.com/BerriAI/litellm)
(MIT). See [ATTRIBUTION.md](ATTRIBUTION.md). Provided as-is for demonstration;
review and harden before production use.
