# Buy or Wait? — HackerRank Orchestrate (September 2026)

## What it does

For every purchase or payment request in `dataset/requests.csv`, this system
rebuilds the user's cash position and writes one recommendation row to
`output.csv`: how much is safe to pay today, an affordability status, a payment
method (full payment, partial payment, installments, wait, or not recommended),
the payment plan, and the earliest date a single full payment is safe. It projects
a conservative 90-day cash ledger from the profile, settled history, pending and
scheduled events, fixed dated exchange rates, supporting messages, and amounts read
from images, then tests every candidate plan against `minimum_balance_to_keep`.
Prediction is deterministic, offline, and makes no model calls.

## Setup

- Python 3.10 or newer.
- Standard library only. Nothing to install, no API keys, no network access.

## Run

Run from the folder that holds `code/` and `tests/`: the unzipped `code.zip`
root or the repository root. Both work the same way.

```bash
python3 code/main.py --predict output.csv              # write predictions
python3 code/main.py --check-output output.csv         # validate dataset + output contract
python3 code/main.py --forecast request_26             # print one request's 90-day ledger
python3 code/evaluation/main.py output.csv             # predict + contract check + sample scoring
python3 -m unittest discover -s tests -q               # test suite
```

`dataset/` must sit next to `code/` and `tests/` (or pass `--dataset DIR` to the
`code/` commands). Other flags: `--validate` (dataset summary and contract checks,
the default action) and `--max-issues N`. `--predict` with no path writes
`output.csv` next to `code/`.

## Zip layout

```text
code.zip
├── AGENTS.md
├── code/
│   ├── README.md
│   ├── main.py
│   ├── cashflow.py ... validation.py
│   ├── evaluation/
│   │   ├── main.py
│   │   └── usage_report.md
│   └── evidence/
│       ├── image_amounts.json
│       ├── image_amounts_usage.json
│       └── resolve_images.py
└── tests/
```

`dataset/` is not included. The dataset is found by walking up from `code/main.py`
to the first folder holding `dataset/requests.csv`, so after unzipping, put
`dataset/` next to `code/`:

```text
unzipped/
├── AGENTS.md
├── code/
├── dataset/      <- copy or symlink the provided dataset here
└── tests/
```

Or pass it explicitly:

```bash
python3 code/main.py --dataset /path/to/dataset --predict /path/to/output.csv
python3 code/evaluation/main.py /path/to/output.csv --dataset /path/to/dataset
```

## Architecture

```text
dataset_loader -> cashflow.forecast_for_request -> engine.recommend -> output.csv
                  (uses recurrence, evidence_policy, image_evidence)
```

| Module | Role |
|---|---|
| `main.py` | CLI: validate, predict, check output, forecast trace |
| `dataset_loader.py` | Reads every CSV into typed records (money as `Decimal`), builds indexes, reports schema and referential issues |
| `cashflow.py` | Builds the 90-day ledger for one request and records blockers |
| `recurrence.py` | Detects recurring series from settled history |
| `evidence_policy.py` | Deterministic rules that read payroll and income messages |
| `image_evidence.py` | Validates the offline image-amount artifact and fills blank event amounts |
| `engine.py` | Safe amount, earliest full-payment date, candidate plans, ranking, output rows |
| `output_schema.py`, `recommendation_schema.py` | Output value and semantic recommendation validators |
| `validation.py` | Shared issue type and formatting |
| `evaluation/main.py` | Evaluation workflow: predict, check the output contract, print the status × method distribution, score the 25 public samples per field (report only) |
| `evidence/resolve_images.py` | One-time offline image resolver (not used at prediction time) |

## Decision policies

- **Opening balance** is `current_available_balance` on `request_date`; the
  forecast runs 90 days.
- **Pending and scheduled debits are reserved.** Pending credits, windfalls
  (bonuses, commissions, lottery), refunds, and unrealized investment gains are
  never counted as cash.
- **Recurring series** come only from settled history: debits are projected at
  their mean amount, credits at their median.
- **Same-day ordering:** credits land before debits.
- **Foreign-currency events** convert at the `exchange_rates.csv` row for the
  settlement date, in the stated direction.
- **Blockers:** a needed event with a blank amount or a missing exchange rate
  stops the forecast, and the request gets a conservative `not_affordable` row.
- **Safety:** a plan is safe only if the balance never falls below
  `minimum_balance_to_keep` at any checkpoint after the plan's payments are booked
  into the ledger.
- **Candidates:** full payment now, wait until the earliest safe date, a two-step
  partial payment, and supplied installment options. Only safe plans that use a
  method the user accepts (installments within `max_installment_months`) and
  finish by the deadline are ranked, by the spec's order: lowest total cost,
  earliest start, fewest payments.

## Evidence handling

**Messages are untrusted data.** `evidence_policy.py` applies narrow,
deterministic template rules to payroll and income messages: payout holds,
amount changes, payday moves, ended income, and first-salary or resumed income.
Instructions inside a message are never obeyed, and a message that only
confirms income never raises a projection above what settled history supports.

**Images are read offline, once.** `evidence/resolve_images.py` sent each image
linked to a blank-amount event to a Gemini vision model and wrote
`evidence/image_amounts.json`. At prediction time `image_evidence.py` accepts a
record only when the event is still blank, the image is the one `images.csv`
links to it, the PNG's SHA-256 matches, and the amount is a plain positive
decimal in the event's currency. Anything else is rejected and the blocker stays.
The local PNG files in `dataset/media/images/` are read and hashed (SHA-256) to
validate every image amount, so a changed or missing file invalidates its record.
Token usage is in `evaluation/usage_report.md`.

Re-running the resolver is optional; the committed artifact is the source of
truth for predictions:

```bash
python3 code/evidence/resolve_images.py --model gemini-3.8-flash-high          # agy backend
python3 code/evidence/resolve_images.py --backend api --model MODEL [--only image_05] [--dry-run]
```

The `api` backend reads `GEMINI_API_KEY` from the environment or a repo-root
`.env` file (never committed).

> **Security caveat for the `agy` backend.** `agy --mode plan --sandbox` blocks
> terminal commands only. Its file tools can still read files anywhere under the
> home directory. Do not re-run the `agy` backend on untrusted images; use the
> `api` backend or an isolated account instead.

## Known limitations

- `image_04` is cut off before its total, so its event amount stays unresolved and
  the affected forecast keeps a blocker.
- One evaluation request can come out incomplete (conservative `not_affordable`)
  because a projected foreign-currency event has no exchange rate for its date.
- Spending changes are not generated; `spending_changes_needed` is always `none`.
- `decision_explanation` text is templated from the forecast and plan, not free-form.
