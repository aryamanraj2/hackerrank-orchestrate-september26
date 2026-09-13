# Model Usage Report — Buy or Wait?

Final full-dataset run: **250 evaluation requests** from `dataset/requests.csv`.

## 1. Where models are used

| Step | When it runs | Model calls | Tokens |
|---|---|---:|---:|
| Prediction (`python3 code/main.py --predict`) | every run | **0** | **0** |
| Image-amount resolution (`code/evidence/resolve_images.py`) | once, offline, before prediction | 16 | 2,767,537 |

The prediction step is deterministic, standard-library Python and makes **no model
or network calls**. It reads the committed artifact `code/evidence/image_amounts.json`,
produced once by the resolver for the 16 `images.csv` rows linked to a financial
event with a blank amount. Exact per-call figures are recorded in
`code/evidence/image_amounts_usage.json`.

## 2. Final run (the one used for `output.csv`)

Resolver settings: prompt version `image-amount-v2`, backend Antigravity CLI
(`agy` 1.2.2), one call per image. Result: 15 of 16 amounts resolved; `image_04`
is cut off before its total and stays unresolved (the forecast keeps a blocker).

### Per model

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens |
|---|---|---:|---:|---:|---:|
| Google | gemini-3.8-flash-high | 16 | 2,503,523 | 264,014 | 2,767,537 |

Notes on the counts:

- `Output tokens` includes 228,628 thinking tokens.
- `Total tokens` = input + output, as reported by agy usage.
- A further 16,727,666 cache-read tokens were reported separately and are not in
  the input or total columns.

### Overall total

| Metric | Value |
|---|---:|
| Model calls | 16 |
| Input tokens | 2,503,523 |
| Output tokens (incl. thinking) | 264,014 |
| Total tokens | 2,767,537 |
| Cache-read tokens (reported separately) | 16,727,666 |
| Average total tokens per request (÷ 250) | 11,070.1 |
| Average total tokens per image call (÷ 16) | 172,971.1 |
| Average model calls per request (÷ 250) | 0.064 |

Tokens were spent per image, not per request; the per-request average spreads
the one-time resolution over the 250 evaluation requests.

## 3. Estimated cost

The runs went through an Antigravity subscription, so there was no per-token bill:
**the actual marginal cost of this run was 0**. The figures below estimate what
the same tokens would cost on pay-as-you-go pricing.

### Assumptions

| Item | Assumed price (USD per 1M tokens) | Status |
|---|---:|---|
| Input tokens | 0.30 | assumed rate — confirm against the provider price list |
| Output tokens (incl. thinking) | 2.50 | assumed rate — confirm against the provider price list |
| Cache-read tokens | 0.03 | assumed rate — confirm against the provider price list |

These are not official prices for `gemini-3.8-flash-high`.

### Formula

```text
cost_io    = input_tokens / 1e6 × input_price + output_tokens / 1e6 × output_price
cost_cache = cache_read_tokens / 1e6 × cache_read_price
cost_total = cost_io + cost_cache
per_request = cost / 250        per_image = cost / 16
```

### Result

| Line | Calculation | Estimated USD |
|---|---|---:|
| Input | 2.503523 × 0.30 | 0.7511 |
| Output | 0.264014 × 2.50 | 0.6600 |
| **Input + output** | | **1.4111** |
| Cache read | 16.727666 × 0.03 | 0.5018 |
| **Total incl. cache read** | | **1.9129** |

| Per unit | Input + output | Incl. cache read |
|---|---:|---:|
| Per request (÷ 250) | 0.00564 | 0.00765 |
| Per image call (÷ 16) | 0.0882 | 0.1196 |
| Prediction step | 0 | 0 |

## 4. Development usage (not used for `output.csv`)

> Figures from development notes, not a machine-readable usage record.

These runs happened while building and calibrating the resolver. None of their
answers feed the submitted predictions. All but the last went through the same
subscription.

| Provider | Model | Purpose | Calls | Total tokens |
|---|---|---|---:|---:|
| Google | gemini-3.1-pro-high (agy) | reference runs over the image set | 3 runs | 2,270,961 |
| Google | gemini-3.8-flash-high (agy) | one discarded full run | 1 run | 1,785,317 |
| Google | agy models | single-image probes | several | ~90,000 |
| Google | gemini-flash-latest (Gemini API) | API backend probe | 1 | 1,838 |
| **Total (approx.)** | | | | **~4,148,116** |

A per-call input/output split was not kept for development runs, so they are not
priced here.
