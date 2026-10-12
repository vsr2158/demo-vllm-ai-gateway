# LiteLLM Gateway — Streamlit Demo Frontend

A lightweight local Streamlit app that showcases the Multi-Provider GenAI
Gateway (LiteLLM on ECS Fargate, Sydney `ap-southeast-2`). It talks to the
gateway's **OpenAI-compatible `/v1` endpoint** using the `openai` Python SDK, so
the same code would work against OpenAI, Azure, or any LiteLLM-fronted provider.

## Three demos

| Tab | What it shows |
|-----|----------------|
| 💬 **Chatbot** | Pick a model, multi-turn chat with streaming + history. |
| ⚖️ **Model comparison** | Send one prompt to N models in parallel; compare replies and latency side-by-side. |
| 🧭 **Complexity routing** | A cheap classifier rates prompt difficulty, then routes SIMPLE→cheap / MODERATE→mid / COMPLEX→premium model — cost-aware routing through one gateway. |

All models are the **verified Sydney Bedrock** set (Claude 4.x/5.x via `au.`
inference profiles + Nova via `apac.`), served by LiteLLM under friendly names
(`claude-sonnet-5-5`, `nova-pro`, etc.).

## Prerequisites

- Python 3.10+
- Network access to the gateway ALB
- A gateway API key (the LiteLLM master key, or a scoped virtual key)

## Setup & run

```bash
cd frontend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp .env.template .env
#  edit .env and set LITELLM_API_KEY (and LITELLM_BASE_URL if the ALB changed)

streamlit run app.py
```

Streamlit opens http://localhost:8501.

## Configuration

Config is read from environment variables (a local `.env` is auto-loaded):

| Var | Default | Notes |
|-----|---------|-------|
| `LITELLM_BASE_URL` | the Sydney ALB DNS | No trailing slash. The app appends `/v1`. |
| `LITELLM_API_KEY` | — (required) | Master key or a virtual key. |

### Using a scoped virtual key (recommended over the master key)

The master key is admin-level. For a safer demo, create a **virtual key** scoped
to specific models + a budget, then use it one of two ways:

**Option 1 — paste it in the app (no restart):** run the app, open the sidebar
**🔑 Connection / API key** panel, paste the key, click **Test** then **Apply**.
This overrides `.env` for the current session only and is never written to disk.

**Option 2 — put it in `.env`:** set `LITELLM_API_KEY=sk-…` so it loads on start.

To create the virtual key:

1. Open the Admin UI: `<LITELLM_BASE_URL>/ui` (login `admin` / master key).
2. **Virtual Keys → + Create New Key**, select the Bedrock models and set a
   budget.
3. Copy the `sk-…` value.

> Note: generating keys via the `/key/generate` REST endpoint through the
> plain-HTTP ALB currently returns a reset connection (an endpoint-specific
> gateway/ALB quirk). The Admin UI key-creation flow works, so use that.

## Troubleshooting

- **"Missing configuration" / key NOT SET** — you didn't set `LITELLM_API_KEY`.
  Edit `.env` or `export LITELLM_API_KEY=…`.
- **`⚠️ error: … model not found`** — the model isn't in the gateway's served
  list; pick one from the dropdown (those are the verified names).
- **Connection errors** — confirm the gateway is up:
  `curl $LITELLM_BASE_URL/health/liveliness` should return `"I'm alive!"`.
- The ALB is plain **HTTP** (demo), so traffic (incl. the key) is unencrypted.
  Fine for a short demo on a non-prod account; don't send anything sensitive.
