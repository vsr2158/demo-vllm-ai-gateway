"""
LiteLLM Gateway — Streamlit demo frontend.

Three demos, all calling the gateway's OpenAI-compatible /v1 endpoint via the
openai SDK:

  1. Chatbot            — pick a model, multi-turn chat with history.
  2. Model comparison   — send one prompt to N models, show replies side-by-side.
  3. Complexity routing — classify the prompt's difficulty, then route it to a
                          cheap model (simple) or a premium model (complex),
                          demonstrating cost-aware routing through one gateway.

Run:
  cd frontend
  python -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt
  cp .env.template .env   # then edit LITELLM_API_KEY
  streamlit run app.py
"""

import time
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
from openai import OpenAI
import gzip
import httpx

import config

st.set_page_config(page_title="LiteLLM Gateway Demo", page_icon="🚪", layout="wide")


# --- Client ------------------------------------------------------------------
class _FixEncodingTransport(httpx.HTTPTransport):
    """Normalize the gateway's inconsistent Content-Encoding handling.

    The gateway tags responses `Content-Encoding: gzip` inconsistently:
      - some bodies are plain JSON (NOT actually compressed), and
      - some bodies ARE really gzipped.
    Both arrive with the same gzip header, so neither "trust the header" nor
    "always strip it" works for every endpoint. Instead we inspect the actual
    bytes: if they start with the gzip magic (1f 8b) we decompress; otherwise we
    pass them through. Either way we drop the now-inaccurate Content-Encoding /
    Content-Length so httpx treats the body as the plaintext it actually is.

    Streaming responses (text/event-stream) are passed through untouched — SSE
    isn't gzipped and must not be buffered.
    """

    def handle_request(self, request):
        response = super().handle_request(request)

        ctype = response.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # Streaming SSE: don't buffer. Just drop a bogus encoding header.
            response.headers.pop("content-encoding", None)
            return response

        # Consume the RAW transport bytes without httpx's content decoder
        # (response.read() would try to gunzip using the bogus header first).
        raw = b"".join(part for part in response.stream)
        try:
            response.stream.close()
        except Exception:
            pass

        if raw[:2] == b"\x1f\x8b":  # real gzip
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass

        response.headers.pop("content-encoding", None)
        response.headers["content-length"] = str(len(raw))
        # Attach the normalized bytes as the new (already-decoded) body.
        response.stream = httpx.ByteStream(raw)
        response._content = raw
        return response


@st.cache_resource
def get_client(base_url: str, api_key: str) -> OpenAI:
    # Cached per (base_url, api_key) so pasting a new key rebuilds the client.
    http_client = httpx.Client(
        transport=_FixEncodingTransport(),
        timeout=httpx.Timeout(60.0),
    )
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=http_client,
    )


def _active_client() -> OpenAI:
    """Client built from the key/base URL currently active in the session."""
    return get_client(
        st.session_state.get("active_base_url", config.OPENAI_BASE_URL),
        st.session_state.get("active_api_key", config.API_KEY),
    )


def _build_kwargs(model, messages, max_tokens, temperature, stream):
    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                  stream=stream)
    # Only send temperature when explicitly provided. Several newer Bedrock
    # Claude models (e.g. au.anthropic.claude-haiku-5-5) reject a non-default
    # temperature with UnsupportedParamsError, so default to omitting it and
    # letting the model use its own default.
    if temperature is not None:
        kwargs["temperature"] = temperature
    return kwargs


def chat_once(model: str, messages: list[dict], max_tokens: int = 512,
              temperature: float | None = None) -> str:
    """Single non-streaming completion. Returns text or an error string."""
    try:
        resp = _active_client().chat.completions.create(
            **_build_kwargs(model, messages, max_tokens, temperature, False)
        )
        return resp.choices[0].message.content or ""
    except Exception as e:  # surface gateway/model errors inline
        return f"⚠️ error: {e}"


def stream_chat(model: str, messages: list[dict], max_tokens: int = 512,
                temperature: float | None = None):
    """Yield content chunks for st.write_stream."""
    try:
        stream = _active_client().chat.completions.create(
            **_build_kwargs(model, messages, max_tokens, temperature, True)
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta
    except Exception as e:
        yield f"⚠️ error: {e}"


# --- Sidebar: connection config ----------------------------------------------
st.sidebar.title("🚪 LiteLLM Gateway")
st.sidebar.caption("Multi-provider GenAI gateway — Sydney Bedrock demo")

# Seed session defaults from config/.env on first load.
if "active_api_key" not in st.session_state:
    st.session_state["active_api_key"] = config.API_KEY
if "active_base_url" not in st.session_state:
    st.session_state["active_base_url"] = config.OPENAI_BASE_URL

with st.sidebar.expander("🔑 Connection / API key", expanded=not config.API_KEY):
    st.caption(
        "Paste a **virtual key** (create one in the Admin UI → Virtual Keys) or "
        "the master key. Overrides the value from `.env` for this session only — "
        "nothing is written to disk."
    )
    key_input = st.text_input(
        "API key",
        value=st.session_state["active_api_key"],
        type="password",
        placeholder="sk-…",
        key="key_input",
    )
    base_input = st.text_input(
        "Gateway base URL",
        value=config.BASE_URL,
        help="Base ALB URL, no trailing slash. '/v1' is appended automatically.",
        key="base_input",
    )

    c_apply, c_test = st.columns(2)
    if c_apply.button("Apply", use_container_width=True):
        st.session_state["active_api_key"] = key_input.strip()
        st.session_state["active_base_url"] = base_input.strip().rstrip("/") + "/v1"
        st.sidebar.success("Applied for this session.")
        st.rerun()

    if c_test.button("Test", use_container_width=True):
        try:
            client = get_client(
                base_input.strip().rstrip("/") + "/v1", key_input.strip()
            )
            models = client.models.list()
            n = len(list(models.data))
            st.sidebar.success(f"OK — {n} models visible to this key.")
        except Exception as e:
            st.sidebar.error(f"Failed: {str(e)[:160]}")

# Live status readout of what's actually in use.
_active_key = st.session_state["active_api_key"]
_active_base = st.session_state["active_base_url"]
st.sidebar.markdown("**Active connection**")
st.sidebar.write(f"Endpoint: `{_active_base}`")
st.sidebar.write(
    "Key: "
    + (f"`{_active_key[:6]}…{_active_key[-4:]}`" if _active_key else "`NOT SET`")
)
if _active_key == config.API_KEY and config.API_KEY:
    st.sidebar.caption("source: `.env` / environment")
elif _active_key:
    st.sidebar.caption("source: pasted in sidebar")

# --- Config guard ------------------------------------------------------------
if not _active_key:
    st.error("No API key set. Open **🔑 Connection / API key** in the sidebar "
             "and paste a virtual key (or the master key), then click Apply.")
    st.stop()

tab_chat, tab_compare, tab_route = st.tabs(
    ["💬 Chatbot", "⚖️ Model comparison", "🧭 Complexity routing"]
)


# =============================================================================
# Tab 1 — Chatbot
# =============================================================================
with tab_chat:
    st.subheader("Chatbot")
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        chat_model = st.selectbox("Model", config.CHAT_MODELS, index=0, key="chat_model")
    with col2:
        use_temp = st.checkbox("Set temperature", value=False, key="chat_use_temp",
                               help="Some newer Claude models reject a custom "
                                    "temperature. Leave off to use the model default.")
        chat_temp = st.slider("Temperature", 0.0, 1.0, 0.7, 0.1, key="chat_temp",
                              disabled=not use_temp)
    with col3:
        chat_max = st.number_input("Max tokens", 64, 4096, 512, 64, key="chat_max")

    if st.button("🗑️ Clear history", key="clear_chat"):
        st.session_state.pop("chat_history", None)

    history = st.session_state.setdefault("chat_history", [])

    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    prompt = st.chat_input("Message the model…")
    if prompt:
        history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            reply = st.write_stream(
                stream_chat(
                    chat_model, history, int(chat_max),
                    float(chat_temp) if use_temp else None,
                )
            )
        history.append({"role": "assistant", "content": reply})


# =============================================================================
# Tab 2 — Model comparison
# =============================================================================
with tab_compare:
    st.subheader("Model comparison")
    st.caption("Send the same prompt to several models through the gateway and "
               "compare replies + latency side-by-side.")
    selected = st.multiselect(
        "Models to compare",
        config.CHAT_MODELS,
        default=config.DEFAULT_COMPARE,
        key="cmp_models",
    )
    cmp_max = st.slider("Max tokens", 64, 2048, 400, 64, key="cmp_max")
    cmp_prompt = st.text_area(
        "Prompt", "Explain what an API gateway is in 2 sentences.", key="cmp_prompt"
    )

    if st.button("▶️ Run comparison", type="primary", key="cmp_run"):
        if not selected:
            st.warning("Select at least one model.")
        else:
            messages = [{"role": "user", "content": cmp_prompt}]

            def _run(model):
                t0 = time.time()
                text = chat_once(model, messages, int(cmp_max))
                return model, text, time.time() - t0

            with st.spinner(f"Querying {len(selected)} models…"):
                with ThreadPoolExecutor(max_workers=min(len(selected), 6)) as ex:
                    results = list(ex.map(_run, selected))

            cols = st.columns(len(results))
            for col, (model, text, dt) in zip(cols, results):
                with col:
                    st.markdown(f"**{model}**")
                    st.caption(f"⏱ {dt:.2f}s")
                    st.markdown(text)


# =============================================================================
# Tab 3 — Complexity routing
# =============================================================================
with tab_route:
    st.subheader("Complexity-based routing")
    st.caption(
        "A cheap classifier model rates the prompt's complexity, then the "
        "request is routed to a cost-appropriate model: simple → cheap, "
        "complex → premium. All through the one gateway."
    )

    c1, c2 = st.columns(2)
    with c1:
        simple_model = st.selectbox(
            "Simple-tier model", config.CHAT_MODELS,
            index=config.CHAT_MODELS.index(config.TIER_SIMPLE), key="rt_simple")
        moderate_model = st.selectbox(
            "Moderate-tier model", config.CHAT_MODELS,
            index=config.CHAT_MODELS.index(config.TIER_MODERATE), key="rt_moderate")
    with c2:
        complex_model = st.selectbox(
            "Complex-tier model", config.CHAT_MODELS,
            index=config.CHAT_MODELS.index(config.TIER_COMPLEX), key="rt_complex")
        classifier_model = st.selectbox(
            "Classifier model (cheap)", config.CHAT_MODELS,
            index=config.CHAT_MODELS.index(config.TIER_SIMPLE), key="rt_classifier")

    rt_prompt = st.text_area(
        "Prompt",
        "Prove that the square root of 2 is irrational.",
        key="rt_prompt",
    )

    # Classifier system prompt (reused + shown for transparency).
    CLASSIFY_SYSTEM = (
        "You are a routing classifier. Rate the user's request complexity as "
        "exactly one word: SIMPLE, MODERATE, or COMPLEX. SIMPLE = greetings/short "
        "factual lookups. MODERATE = explanations, summaries, light reasoning. "
        "COMPLEX = multi-step reasoning, proofs, code design, hard analysis. "
        "Respond with ONLY the one word."
    )

    tier_models = {
        "SIMPLE": simple_model,
        "MODERATE": moderate_model,
        "COMPLEX": complex_model,
    }

    if st.button("▶️ Classify & route", type="primary", key="rt_run"):
        # ---------------------------------------------------------------
        # STEP 1 — classify complexity with the cheap classifier model.
        # ---------------------------------------------------------------
        st.markdown("### Step 1 · Classify")
        st.caption(f"Cheap classifier (`{classifier_model}`) reads the prompt and "
                   "returns one word: SIMPLE / MODERATE / COMPLEX.")
        classify_msgs = [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": rt_prompt},
        ]
        t0 = time.time()
        with st.spinner(f"Classifying with {classifier_model}…"):
            verdict_raw = chat_once(classifier_model, classify_msgs,
                                    max_tokens=5).strip().upper()
        classify_dt = time.time() - t0

        verdict = "MODERATE"
        for v in ("COMPLEX", "SIMPLE", "MODERATE"):
            if v in verdict_raw:
                verdict = v
                break
        routed_model = tier_models[verdict]

        colA, colB, colC = st.columns(3)
        colA.metric("Complexity", verdict)
        colB.metric("Routed to", routed_model)
        colC.metric("Classify latency", f"{classify_dt:.2f}s")
        st.caption(f"Classifier raw output: `{verdict_raw}`")

        st.divider()

        # ---------------------------------------------------------------
        # STEP 2 — generate the answer with the routed tier model.
        # ---------------------------------------------------------------
        st.markdown(f"### Step 2 · Generate")
        st.caption(f"Prompt routed to the **{verdict}** tier model "
                   f"(`{routed_model}`), which produces the answer.")
        with st.chat_message("assistant"):
            st.write_stream(
                stream_chat(routed_model,
                            [{"role": "user", "content": rt_prompt}],
                            max_tokens=800)
            )
