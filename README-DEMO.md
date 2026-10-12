# GenAI Gateway — Demo Runbook

This is the **demo layer** for the Multi-Provider GenAI Gateway. It is an
**optional, additive** Streamlit app (`frontend/`) that runs on top of an
**already-deployed gateway** (Part 1) and showcases its capabilities.

> **Prerequisite:** deploy the gateway first (see the main
> [`README.md`](README.md), Part 1). You need the gateway's **ServiceURL** and
> an **API key** (master key or a scoped virtual key). The demo talks to the
> gateway's OpenAI-compatible `/v1` endpoint — it does not deploy any AWS infra
> of its own.

The demo is deliberately separate from the gateway so the gateway can be used as
a standalone capability. Some demo features require **post-Phase-1 configuration
on the gateway/LiteLLM side** — those are tracked under
[Feature config](#feature-config-post-phase-1) below and filled in as each
feature is built.

---

## What's different from the upstream project

| Aspect | Upstream guidance | This demo edition |
|--------|-------------------|-------------------|
| **IaC** | Terraform | **AWS CDK v2 (Python)** |
| **Container platform** | ECS Fargate *or* EKS | **ECS Fargate only** (EKS dropped) |
| **Region** | 15+ supported regions | **Sydney (`ap-southeast-2`)**, demo-tuned |
| **Edge** | CloudFront default | **Direct ALB by default**, CloudFront as a toggle |
| **Goal** | Production reference | **Fast demos** — minimal footprint, quick teardown |
| **Frontend** | Admin UI only | Admin UI **+ a lightweight Streamlit demo app** (Phase 2) |
| **Deploy flow** | `deploy.sh` (region autodetect, yq merge, `terraform apply`) | `cdk deploy` |

Everything that gives the gateway its value is **kept**: LiteLLM as the unified
OpenAI-compatible engine, the Bedrock FastAPI middleware, virtual API keys,
budgets/rate-limits, RDS + Redis, Secrets Manager, and the full auth model.

---

## Why "lean, demo-focused"?

The upstream solution is excellent but heavy to stand up for a quick showcase:
two orchestration platforms, CloudFront + Route53 + ACM wiring, and a large
Terraform surface. For a demo you usually want to:

- **Deploy in minutes, tear down in minutes** — minimal moving parts.
- **Run in one region** (Sydney) against Amazon Bedrock.
- **Show capabilities fast** — chat, model comparison, smart routing.
- **Keep baseline cost low** while idle, and delete cleanly when done.

So this edition drops EKS (37 resources), drops the deployment telemetry stack,
makes CloudFront optional, and expresses the whole thing as a small set of CDK
stacks.

---

## Architecture (demo configuration)

```
          Client (Streamlit app / curl / SDK)
                        │  Authorization: Bearer sk-...
                        ▼
             Application Load Balancer (WAF)
                        │  path routing
        ┌───────────────┴────────────────┐
        ▼                                 ▼
  LiteLLM proxy  :4000            Bedrock middleware :3000
  (virtual keys, routing,        (Bedrock-native interface,
   budgets, caching)              chat history, Okta JWT opt.)
        │                                 │
        ▼                                 ▼
  RDS Postgres   ElastiCache Redis   Amazon Bedrock (Claude, Nova, …)
  (keys/usage)   (cache/settings)    Secrets Manager · S3 logs · KMS
```

CDK stacks:

```
cdk/
  app.py
  config.py                 # Sydney defaults + sizing (was .env + variables.tf)
  build_config.py           # merges base + region model lists -> assets/config.yaml
  stacks/
    network_stack.py        # VPC, subnets, NAT, VPC endpoints, flow logs
    data_stack.py           # RDS, Redis, S3 logs/config, Secrets, KMS
    gateway_stack.py        # ECR/image, ECS Fargate, ALB + rules, (opt) CloudFront
```

---

## Status

Both phases are **built, deployed, and verified** in `ap-southeast-2`:

- **Phase 1 — Gateway on CDK:** all three stacks deploy; the ECS service runs
  healthy on ARM64/Graviton Fargate; LiteLLM `/health/liveliness` and the
  middleware `/bedrock/health/liveliness` both return 200.
- **Phase 2 — Streamlit demo app:** `frontend/` with three tabs (chatbot, model
  comparison, complexity routing), verified end-to-end against the live gateway.

---

## Capabilities

**Phase 1 — Gateway on CDK (Sydney)**
- Ported the Terraform `base` + `ecs` modules to Python CDK (EKS dropped).
- Targets `ap-southeast-2`; direct-ALB demo path with a CloudFront toggle.
- Reuses the LiteLLM + Bedrock-middleware containers.

**Phase 2 — Streamlit demo app** (`frontend/`)
- **Chatbot** — pick a Bedrock model and chat (streaming + history).
- **Model comparison** — one prompt fanned out to several models, responses
  side-by-side with latency.
- **Complexity-based routing** — a cheap classifier model rates the prompt
  SIMPLE / MODERATE / COMPLEX, then routes to a cost-appropriate model
  (Nova/Haiku for simple, Sonnet/Opus for complex), shown as two explicit steps.

---

## Region & models

- **Region:** `ap-southeast-2` (Sydney) — 3 AZs, Bedrock available.
- **Model config:** `config/default-config-ap-southeast-2.yaml` (Bedrock models)
  + `config/default-config-base.yaml` (non-Bedrock providers). Run
  `python cdk/build_config.py ap-southeast-2` to regenerate the merged
  `cdk/assets/config.yaml`.
- **Bedrock models (verified active in Sydney):** the region config exposes only
  models confirmed invokable in-account, preferring `au.*` (Australia-local)
  then `apac.*` inference profiles:
  - **Claude** (`au.*`): sonnet-4-5 / 4-6 / 5 / 5-5, haiku-4-5 / 5-5,
    opus-4-6 / 4-7 / 4-8 / 5 / 5-5
  - **Amazon Nova** (`apac.*`): micro, lite, pro
  - **Embeddings:** Titan v2 (text + image), Cohere embed english / multilingual
  > Legacy direct `anthropic.claude-3.x` ids were removed — Bedrock marks them
  > Legacy / inference-profile-only, so they 404 in this account.
- **Prerequisite:** enable access to the required models in the Bedrock console
  for the account/region before deploying.

---

## Prerequisite: a deployed gateway (Part 1)

The demo runs on top of an already-deployed gateway. If you haven't deployed it,
follow **Part 1** in the main [`README.md`](README.md) (`cdk deploy …`). You
need two things from that deployment:

- **ServiceURL** — the gateway ALB endpoint (a CDK stack output).
- **API key** — the master key (from the `MasterKeySecretArn` secret) or a
  scoped virtual key created in the Admin UI (`<ServiceURL>/ui`).

Verify the gateway is up before running the demo:
```bash
curl $SERVICE_URL/health/liveliness            # LiteLLM   -> "I'm alive!"
curl $SERVICE_URL/bedrock/health/liveliness    # middleware -> {"status":"healthy",...}
```

---

## Streamlit demo app (Phase 2)

```bash
cd frontend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env           # set LITELLM_API_KEY (master key or a virtual key)
streamlit run app.py            # http://localhost:8501
```

- The sidebar **🔑 Connection / API key** panel lets you paste a key (master or a
  virtual `sk-...`) and point at a different gateway URL at runtime — no restart.
- Create scoped **virtual keys** in the Admin UI (`<ServiceURL>/ui` →
  Virtual Keys → + Create New Key); copy the `sk-...` shown once at creation.

### Known gateway quirks (handled)
- The gateway can tag responses `Content-Encoding: gzip` inconsistently (some
  bodies aren't actually gzipped). The Streamlit client normalizes this with a
  content-aware httpx transport.
- Some newer Claude profiles reject a custom `temperature`; the app omits it by
  default (opt-in checkbox in the chatbot tab).

---

## Features showcased

The demo exists to show off the gateway. Features fall into three tiers:

| Feature | Status | Demo tab |
|---|---|---|
| Unified multi-provider API | ✅ enabled | Chatbot, Comparison |
| Intelligent / cost-based routing | ✅ enabled | Complexity routing |
| Caching (identical-prompt) | ✅ enabled | planned |
| Budgets & rate limits | ✅ enabled | planned |
| Virtual keys | ✅ enabled | Sidebar |
| Guardrails (Bedrock) | ⚙️ needs config | planned |
| Fallbacks / resiliency | ⚙️ needs config | planned |
| Spend tracking | ⚙️ needs config | planned |
| Semantic caching | ⚙️ needs config | planned |
| **MCP (Model Context Protocol)** | 🔜 placeholder (future) | TBD |
| **Agents** | 🔜 placeholder (future) | TBD |

---

## Feature config (post-Phase-1)

Config the gateway/LiteLLM needs *after* Part 1 is deployed, per demo feature.
Sections marked **TBD** are filled in as we build each one.

### Caching  ✅ enabled
Redis response caching is already on (`litellm_settings.cache: True`). No gateway
config needed — the demo just needs to surface cache-hit metadata (UI pending).

### Budgets & rate limits  ✅ enabled
Create a scoped virtual key with a small `max_budget` (and optional
`rpm_limit`/`tpm_limit`) in the Admin UI or via `/key/generate`, then use it in
the demo to show enforcement (requests blocked once the budget is spent). No
redeploy needed.

### Guardrails  ⚙️ needs config
1. Create a **Bedrock Guardrail** (block topics, PII masking, content filters).
2. Reference its `guardrailIdentifier` / `guardrailVersion` under `guardrails:`
   in `config/default-config-base.yaml` (a commented stub exists there).
3. Regenerate config (`python cdk/build_config.py ap-southeast-2`), redeploy the
   data stack, and restart the service so LiteLLM reloads the config.
   *(Exact steps / guardrail definition: TBD — added as we build this tab.)*

### Fallbacks / resiliency  ⚙️ needs config
Define `fallbacks` / `context_window_fallbacks` / `default_fallbacks` under
`router_settings` in `config/default-config-base.yaml` (commented stubs exist),
then regenerate + redeploy config and restart. *(Exact config: TBD.)*

### Spend tracking  ⚙️ needs config
LiteLLM writes per-request spend to RDS. The demo would read spend-by-key/model
via the LiteLLM admin endpoints. *(Exact endpoints / UI: TBD.)*

### Semantic caching  ⚙️ needs config
Switch cache to `redis-semantic` with a `similarity_threshold` + an embedding
model (commented stub in `config/default-config-base.yaml`). *(Exact config:
TBD.)*

### MCP (Model Context Protocol)  🔜 placeholder
Expose/proxy MCP tool servers through the gateway. **Future** — configuration
and demo surface to be defined later.

### Agents  🔜 placeholder
Multi-step agentic / tool-use workflows on top of the gateway. **Future** —
configuration and demo surface to be defined later.

---

## Security notes

- Client access always requires a **LiteLLM virtual key** (`sk-...`), regardless
  of edge configuration.
- The **master key** and **salt key** are generated at deploy time and stored in
  **AWS Secrets Manager** (never in source).
- The Fargate **task role** grants Bedrock/Secrets/S3 access — no static AWS
  credentials in containers.
- Direct-ALB demo mode exposes the ALB publicly but it is useless without a
  valid API key; enable the **CloudFront toggle** (secret-header auth + Shield)
  or **WAF** for a hardened posture.
- Optional **Okta OAuth2 JWT** auth is available via the middleware (off by
  default).

---

## Attribution

Adapted from the AWS Solutions Library guidance *Multi-Provider Generative AI
Gateway on AWS* (MIT-0). Built on [LiteLLM](https://github.com/BerriAI/litellm).
This demo edition is provided as-is for demonstration purposes; review and
harden before any production use.
