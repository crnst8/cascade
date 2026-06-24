<div align="center">
  <img src="cascade.png" width="92" alt="cascade" />

  # cascade
  ### Auto-switching CLI chat across free API tiers
</div>

---

`cascade` is a single Python script that routes chat requests across free-tier API providers.

It finds available models from your configured providers, ranks them, and sends each message to the best available model. If a model hits a rate limit, quota, or temporary error, cascade tries the next available model automatically.

No dependencies are required beyond Python 3.

## Providers

Providers are enabled automatically when their API key is added to `.env`.

| Provider | Env var | Free limit | Get a key |
| --- | --- | --- | --- |
| Groq | `GROQ_KEY` | ~1,000 requests/day per model | [console.groq.com](https://console.groq.com) |
| Cerebras | `CEREBRAS_KEY` | 30 RPM, 1M tokens/day | [cloud.cerebras.ai](https://cloud.cerebras.ai/) |
| Google Gemini | `GEMINI_KEY` | 250-1,500 requests/day | [aistudio.google.com](https://aistudio.google.com) |
| Mistral | `MISTRAL_KEY` | 1 request/sec, 1B tokens/month | [console.mistral.ai](https://console.mistral.ai/) |
| SambaNova | `SAMBANOVA_KEY` | $5 trial / 3 months | [cloud.sambanova.ai](https://cloud.sambanova.ai/) |
| Nvidia NIM | `NIM_KEY` | 40 RPM, 1K-5K credits | [build.nvidia.com](https://build.nvidia.com) |
| Cloudflare | `CF_ACC_ID` + `CF_API_TOKEN` | 10,000 neurons/day | [developers.cloudflare.com/workers-ai](https://developers.cloudflare.com/workers-ai) |
| OpenRouter | `OR_KEY` | 20 RPM, 50-1,000 requests/day for `:free` models | [openrouter.ai](https://openrouter.ai) |
| OVHcloud | none | 2 RPM, no key required | [endpoints.ai.cloud.ovh.net](https://endpoints.ai.cloud.ovh.net) |
| Scaleway | `SCALEWAY_KEY` | 1M tokens | [console.scaleway.com](https://console.scaleway.com/generative-api/models) |
| Nebius | `NEBIUS_KEY` | $1 trial | [tokenfactory.nebius.com](https://tokenfactory.nebius.com/) |
| Hyperbolic | `HYPERBOLIC_KEY` | $1 trial | [app.hyperbolic.ai](https://app.hyperbolic.ai/) |
| DeepInfra | `DEEPINFRA_KEY` | 200 concurrent | [deepinfra.com](https://deepinfra.com/login) |
| Fireworks | `FIREWORKS_KEY` | $1 trial | [fireworks.ai](https://fireworks.ai/) |
| Novita | `NOVITA_KEY` | $0.50 trial / 1 year | [novita.ai](https://novita.ai/) |
| SiliconFlow | `SILICONFLOW_KEY` | 1K RPM, 50K TPM | [cloud.siliconflow.cn](https://cloud.siliconflow.cn/account/ak) |
| Z.AI / GLM | `ZAI_KEY` | free tier | [z.ai](https://z.ai) |
| Chutes AI | `CHUTES_KEY` | community GPU | [chutes.ai](https://chutes.ai) |

Run this to check connected providers and available setup details:

```bash
python3 cascade.py --providers
```

Inside chat, use:

```text
/providers
```

After adding or changing keys, run:

```text
/refresh
```

OVHcloud works without a key, so cascade can run even with an empty `.env`.

## Setup

Create a `.env` file in the same folder as `cascade.py`.

Add any provider keys you want to use:

```env
GROQ_KEY=...
GEMINI_KEY=...
MISTRAL_KEY=...
```

Run cascade with Python 3:

```bash
python3 cascade.py
```

No `pip install` step is required.

## Usage

```bash
python3 cascade.py             # interactive chat
python3 cascade.py --list      # ranked model list
python3 cascade.py --providers # provider status and setup details
python3 cascade.py --bench     # test model speed
python3 cascade.py -q "..."    # one-shot prompt
python3 cascade.py --serve     # OpenAI-compatible HTTP server
```

## Python usage

```python
import cascade

reply = cascade.ask("Hello", system="Be concise.")
print(reply)
```

`cascade.ask()` uses the same routing and failover logic as the CLI. It returns only the completion text.

## Chat commands

| Command | Description |
| --- | --- |
| `/models` | Show ranked models with live status |
| `/providers` | Show connected and available providers |
| `/usage` | Show daily usage and rate-limit state |
| `/bench` | Test top models for latency and tokens/sec |
| `/fastest` | Rank by measured speed after running `/bench` |
| `/quality` | Restore the default quality ranking |
| `/use <n>` | Pin to model index `n` |
| `/auto` | Resume automatic routing |
| `/system <text>` | Set the system prompt |
| `/clear` | Clear conversation history |
| `/refresh` | Re-discover models and clear temporary cooldowns |
| `/help` | Show commands |
| `/quit` | Exit |

## Server mode

Run cascade as an OpenAI-compatible local server:

```bash
python3 cascade.py --serve
```

Default address:

```text
http://127.0.0.1:8000
```

Use a custom host or port:

```bash
python3 cascade.py --serve --host 0.0.0.0 --port 9000
```

You can also configure the server with environment variables:

```env
CASCADE_HOST=127.0.0.1
CASCADE_PORT=8000
CASCADE_API_KEY=your-local-key
```

If `CASCADE_API_KEY` is set, requests must include:

```http
Authorization: Bearer your-local-key
```

If it is not set, any API key is accepted. This is intended for local use.

## Server endpoints

| Method and path | Description |
| --- | --- |
| `POST /v1/chat/completions` | Chat completions with routing and failover. Supports `stream: true`. |
| `GET /v1/models` | Discovered models, plus the `auto` model |
| `GET /v1/providers` | Provider status, limits, signup links, and local request counts |
| `POST /v1/refresh` | Re-discover models and clear temporary cooldowns |
| `GET /health` | Server health and provider summary |

## Model selection

Use `model` to choose the routing mode.

```json
{
  "model": "auto"
}
```

`auto` uses the best available model and falls back to others when needed.

```json
{
  "model": "groq/openai/gpt-oss-120b"
}
```

A full `provider/model` value pins the request to that model.

```json
{
  "model": "some-model-id"
}
```

A bare model ID can use any provider offering that model. Cascade chooses the best available provider for it.

## curl example

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## OpenAI Python SDK example

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed",
)

stream = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
    stream=True,
)

for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Successful responses include a non-standard `cascade` field showing which provider and model handled the request. It also lists any failover attempts.

## Ranking

Cascade ranks discovered models using:

- model family
- parameter size
- provider speed

Cerebras and Groq are preferred as speed tiebreakers. The 70B-200B range is favoured for free-tier use. Models at 300B or above are ranked lower by default because they are often slower or more restricted on free tiers.

Models marked as ready are used first. Models on cooldown or marked down are skipped.

Use `/bench` and `/fastest` to route by measured speed instead.

## Benchmarking

Run:

```text
/bench
```

Cascade sends a small prompt to the top available models at the same time, then reports:

- first-token latency
- tokens per second
- result status

After benchmarking, run:

```text
/fastest
```

This ranks models by measured throughput instead of the default ranking.

## Failover behaviour

| Signal | Action |
| --- | --- |
| HTTP 429 or rate limit | Cool down the model, then try the next one |
| HTTP 402, quota, or credit error | Apply a long cooldown, then try the next one |
| HTTP 401 or 403 | Disable the provider for the session |
| HTTP 400, 404, or 422 | Mark the model as unsupported, then try the next one |
| HTTP 5xx, network error, or timeout | Apply a short cooldown, then try the next one |

## Usage tracking

Cascade stores local usage state in:

```text
~/.cascade_state.json
```

It tracks:

- daily request counts
- documented free limits
- temporary cooldowns
- long cooldowns from quota exhaustion

Provider-specific handling:

| Provider | Tracking |
| --- | --- |
| OpenRouter | Live tier and daily spend from the `auth/key` endpoint |
| Groq | Remaining requests and tokens from response headers |
| Other providers | Local request counts and failover on rate-limit or quota errors |
