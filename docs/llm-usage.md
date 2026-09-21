# LLM usage and cost accounting

Polaris records each model call in the server-side usage ledger and summarizes it by provider,
model, research stage, and day. The dashboard follows the useful parts of CC Switch's usage view:
input and output tokens, cache activity, model attribution, and estimated USD cost are visible in one
place. This is a product-design reference rather than a CC Switch data import; Polaris measures calls
that pass through its own `app/core/llm/` boundary.

## Where to find it

- Every signed-in user can open **Settings → Usage** (`/settings?tab=myusage`) to see their own calls.
- The deployment owner can open **Settings → Usage overview** (`/settings?tab=usage`) to see all
  recorded calls and filter the window to 7, 30, or 90 days.
- Every user can configure prices for providers in their own configuration scope under
  **Settings → Models & routing** (`/settings?tab=llm`), in the **Model pricing** card. For the
  deployment owner this is the shared deployment configuration. Prices are stored per provider and
  exact model ID, in USD per million tokens.

![Usage dashboard grouped by model and day](assets/llm-usage-dashboard.png)

## Token and cache semantics

`prompt_tokens` is the complete input reported for a call. It already includes cache reads and cache
writes, so the dashboard never adds either cache bucket to input a second time. Output is stored
separately as `completion_tokens`.

When a provider reports both cache buckets, Polaris records:

- **cache read tokens**: input served from the provider's prompt cache;
- **cache creation tokens**: input written to the cache for possible reuse;
- **cache hit rate**: cache read tokens divided by complete input tokens.

A missing cache field means “not reported,” not zero. The dashboard therefore shows cache reporting
coverage alongside the aggregate. A dash means the value is unknown; it does not mean that the call
had no cache hit. When coverage is partial, the read count contains known values only while the hit
rate still divides by all input; the displayed rate is therefore a conservative lower bound.

## Prices, costs, and snapshots

Each provider has an exact model-ID price table with four possible rates:

| Rate | Meaning |
| --- | --- |
| Input | Input that was neither read from nor written to cache |
| Output | Generated output |
| Cache read | Input served from cache |
| Cache creation | Input written to cache |

Input and output prices are required for a configured model. Cache prices are optional: leaving one
blank means unknown, while entering `0` explicitly means free. Polaris calculates one call as:

```text
fresh input = input - cache read - cache creation
cost = fresh input × input rate
     + output × output rate
     + cache read × cache-read rate
     + cache creation × cache-creation rate
```

Every term is divided by one million. If a required token bucket or rate is unavailable, or the
buckets are inconsistent, that call's cost remains unknown. Unknown calls are excluded from the
displayed USD sum and are shown through priced-call coverage; they are never silently counted as
free.

At call time Polaris saves both the computed `cost_usd` and a `pricing_snapshot` containing the exact
model ID, currency, and rates used. Editing a provider's prices affects future calls only, so a later
price change cannot rewrite historical costs.

![Per-provider model pricing editor](assets/llm-model-pricing.png)

## Reported and estimated usage

Chat providers normally return token counts. When they do not, Polaris estimates input and output
from text length and marks the ledger row as estimated. Embedding calls are estimated from their input;
reranking uses provider-reported billed tokens when available and otherwise estimates them. The
dashboard reports how many calls contain estimated token counts so totals are not presented with
false precision.

Rows created before this accounting was introduced keep their original input and output totals. Their
provider, cache buckets, cost, and price snapshot remain unknown, and they are marked as estimated.

## Design reference

The cache buckets, hit-rate formula, and four-rate price model follow the usage-accounting design in
[CC Switch](https://github.com/farion1231/cc-switch/tree/fdbe3a85b269ed40695ded5981b6ba8288d30ac3),
particularly its
[usage parser](https://github.com/farion1231/cc-switch/blob/fdbe3a85b269ed40695ded5981b6ba8288d30ac3/src-tauri/src/proxy/usage/parser.rs),
[cost calculator](https://github.com/farion1231/cc-switch/blob/fdbe3a85b269ed40695ded5981b6ba8288d30ac3/src-tauri/src/proxy/usage/calculator.rs),
and
[aggregate statistics](https://github.com/farion1231/cc-switch/blob/fdbe3a85b269ed40695ded5981b6ba8288d30ac3/src-tauri/src/services/usage_stats.rs).
Polaris keeps its own tenant attribution, stage attribution, and price snapshots because all model
calls already pass through its server-side LLM boundary.
