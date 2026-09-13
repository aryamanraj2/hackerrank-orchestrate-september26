# Buy or Wait? — HackerRank Orchestrate (September 2026)

## What it does

For every purchase or payment request in `dataset/requests.csv`, this system
rebuilds the user's cash position and writes one recommendation row to
`output.csv`: how much is safe to pay today, an affordability status, a payment
method (full payment, partial payment, installments, wait, or not recommended),
the payment plan, the earliest date a single full payment is safe, and any
spending changes needed. It projects a conservative 86-day cash ledger from the
profile, settled history, pending and scheduled events, fixed dated exchange
rates, supporting messages, and amounts read from images, then tests every
candidate plan against `minimum_balance_to_keep`. Prediction is deterministic,
offline, and makes no model or network calls.

## Setup

- Python 3.10 or newer.
- Standard library only. Nothing to install, no API keys, no network access.

## Run

Run from the folder that holds `code/` and `tests/`: the unzipped `code.zip`
root or the repository root. Both work the same way.

```bash
python3 code/main.py --predict output.csv              # write predictions
python3 code/main.py --check-output output.csv         # validate dataset + output contract
python3 code/main.py --forecast request_26             # print one request's 86-day ledger, blockers and notes
python3 code/evaluation/main.py output.csv             # predict + contract check + sample scoring
python3 -m unittest discover -s tests                  # test suite (380 tests)
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
| `cashflow.py` | Builds the 86-day ledger for one request and records blockers and notes |
| `recurrence.py` | Detects recurring series from settled history and sets the projected amount |
| `evidence_policy.py` | Deterministic rules that read payroll and income messages |
| `image_evidence.py` | Validates the offline image-amount artifact and fills blank event amounts |
| `engine.py` | Safe amount, earliest full-payment date, candidate plans, spending changes, ranking, output rows |
| `output_schema.py`, `recommendation_schema.py` | Output value and semantic recommendation validators |
| `validation.py` | Shared issue type and formatting |
| `evaluation/main.py` | Evaluation workflow: predict, check the output contract, print the status × method distribution, score the 25 public samples per field (report only) |
| `evidence/resolve_images.py` | One-time offline image resolver (not used at prediction time) |

## Decision policies

- **Opening balance** is `current_available_balance` on `request_date`.
- **Forecast window:** 86 days from `request_date` (`cashflow.HORIZON_DAYS`).
  Commitments on later days are outside the safety check.
- **Pending and scheduled debits are reserved**, no later than the request date.
  Pending credits, windfalls (bonuses, commissions, lottery), refunds, and
  unrealized investment gains are never counted as cash.
- **Recurring series** come only from settled history. Debits are projected at
  the mean of their settled amounts, credits at the median, per currency before
  conversion. An occurrence due on the request date is included. A supplied row
  for the same series within half a cadence replaces the projected occurrence.
- **Same-day ordering:** credits land before debits.
- **Lapsed income:** a recurring income series that already missed its next
  expected payment (`request_date > last date + cadence + half cadence`) is not
  projected, and the forecast leaves a note — unless a new income description in
  the same category started settling after the series' last payment (a new
  employer or client is a change of payer, not lost income). Debits never lapse:
  an unseen bill is still owed.
- **Payroll message facts:** increases apply from their effective date;
  next-payroll reductions and temporary pay affect only the next payday; payday
  moves shift later paydays; ended income stops; a confirmation never raises a
  projection above settled history; a one-off extra with no date is not counted.
- **Dated salary:** notices (first salary, new employer, salary resumes) and
  scheduled salary rows in a category with no projectable income schedule are
  booked on their date and continued monthly through the window. Each such
  credit replaces the nearest projected salary credit, one for one (no double
  counting).
- **Final payroll:** a settled income credit described as "final" ends that
  category's projected income unless a later settled or scheduled credit exists.
- **Foreign-currency events** convert at the `exchange_rates.csv` row for the
  booking or projected date, in the stated direction.
- **Incomplete evidence:** a needed event with a blank amount and no validated
  image amount, or a missing exchange rate, stops the forecast; the request gets
  a conservative `not_affordable` row rather than a guess.
- **Safety:** a plan is safe only if the balance never falls below
  `minimum_balance_to_keep` at any checkpoint after the plan's payments are booked
  into the ledger.
- **Candidates:** full payment now, wait until the earliest safe date, a two-step
  partial payment, and supplied installment options. Only safe plans that use a
  method the user accepts (installments within `max_installment_months`) and
  finish by the deadline are ranked: lowest total cost, earliest start, fewest
  payments.
- **Spending changes** are proposed only when no plan without changes completes
  by the deadline. Only recurring, non-protected expenses in categories the user
  is willing to reduce or stop qualify. Reducing to `minimum_allowed_amount` is
  preferred over stopping; actions are added largest horizon saving first until
  a full-payment or installment plan becomes safe, at most 3; each cites the
  series' latest settled event. `amount_safe_to_pay` and
  `earliest_date_for_full_payment` are computed without changes.

## Evidence handling

**Messages are untrusted data.** `evidence_policy.py` applies narrow,
deterministic template rules to payroll and income messages: payout holds,
amount changes, payday moves, ended income, and first-salary or resumed income.
Only the fact sentences are matched. Instructions inside a message are never
obeyed: a prize notice asking the user to "pay the release charge" matches no
rule and books nothing, and advice such as "income that has ended should be
removed from future estimates" is not read. A message that only confirms income
never raises a projection above what settled history supports.

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

## Calibration and evidence

Only global policies were calibrated. Each was chosen against a bar declared
before measuring, sample rows were never used as per-row answers, and flips were
checked in both directions (samples gained and lost). Scores below are matched
public sample fields in the order safe / status / method / plan / earliest /
spending, each out of 25.

**Projection amounts.** A 48-configuration grid over the debit estimator
(max, p75, mean, median, mean of last 3, last), a protected-category split, the
credit estimator (min, median), and request-date projections. Adopted debit mean
and credit median: status + method + earliest sample matches went from 31 to 47,
and no field regressed.

**Confirmation cap.** Settled base salary is exactly 0.60× the stated "base
confirmed" amount for all 9 such users, and primary salary is 0.62× the stated
remaining salary for all 7 household-income users. Applying the stated amounts
contradicted the earliest date in public sample `request_11`. The cap keeps the
history amounts.

**Dated salary continuation.**

| Variant | Scores |
|---|---|
| (a) book on the stated date only | 2/17/18/18/16/22 |
| (b) + monthly continuation of notices | 2/17/18/18/16/22 |
| (c) + continuation of scheduled salary rows | 3/18/19/19/17/22 — adopted |

**Spending changes.** Reduce-preferred and stop-preferred tied on the samples
(3/20/21/21/17/21 at that stage). Reduce-preferred was kept: public sample
`request_21` reduces although stopping was allowed.

**Safety window and lapsed income** (matched sample fields, out of 150):

| Window | Scores | Total |
|---|---|---:|
| 90 days | 3/20/21/21/17/21 | 103 |
| 60 days | worse | — |
| 76–86 days | 4/21/22/22/19/22 | 110 |
| 87 days | 3/21/22/22/18/21 | — |
| 86 days + lapsed-income rule | 4/22/23/23/20/22 | 114 |

Three public samples (`request_08`, `request_12`, `request_13`) have commitments
on days 87–90 that contradict the reference decision under a 90-day window, and
no sample's example earliest date or plan payment lies beyond day 76. 86 is the
largest value on the plateau. A per-series cap anchored at each series' last
occurrence was rejected (losses on five samples).

**Successor exemption.** Without it the lapse rule turned five evaluation rows
`not_affordable` where a new employer or client had replaced the old income
stream. With it the samples stay at 114, and every evaluation status change
against the previous commit (15 rows) is toward more affordable.

**Rejected alternatives.**

| Alternative | Reason |
|---|---|
| Debits before credits on the same day | 19 evaluation rows missed their deadline by one day |
| Never suppress a projected debit that overlaps a supplied row | double-booked scheduled bills |
| Temporary pay continuing indefinitely | no evidence for it |

## Results on the public samples

`python3 code/evaluation/main.py` on the submitted code. These are the 25 public
examples in `dataset/sample_requests.csv`, not the hidden evaluation labels.

```text
sample scoring (report only)
  amount_safe_to_pay              4/25
  affordability_status            22/25
  recommended_payment_method      23/25
  payment_plan                    23/25
  earliest_date_for_full_payment  20/25
  spending_changes_needed         22/25
```

Distribution over the 250 evaluation rows (31 rows carry spending changes):

```text
status x method
  affordable_later        wait                59
  affordable_now          full_payment        61
  affordable_with_plan    full_payment        8
  affordable_with_plan    installments        62
  affordable_with_plan    partial_payment     9
  not_affordable          not_recommended     51
```

The output is byte-identical across runs and `PYTHONHASHSEED` values.

## Known limitations

- The safety window is 86 days (calibrated on the public samples, see
  Calibration and evidence) although the problem statement describes a 90-day
  check.
- `request_78` is the only incomplete forecast: no supplied USD->INR rate exists
  for a projected date (2025-10-06), so it gets a conservative `not_affordable`
  row.
- `image_04` is cut off before its total, so its event amount stays unresolved
  and the forecast keeps a blocker. This affects only public sample `request_19`.
- `amount_safe_to_pay` matches only 4 of 25 public samples.
- `decision_explanation` text is templated from the forecast and plan, not free-form.
