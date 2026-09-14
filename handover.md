# Buy or Wait? — Builder Handover (written 2026-09-13 14:22 IST)

You are taking over the **builder** role (Claude Code) for a 24-hour hackathon submission. You have shell and file access to the repository but **none of the previous conversation**. This file is the whole context. Read all of it, then read `AGENTS.md` and `problem_statement.md`, before you touch anything.

- Repository: `/Users/aryamanjaiswal/Downloads/Github_pulls/hackerrank-orchestrate-september26`, branch `main`.
- Deadline: **2026-09-13 18:00 IST** (`2026-09-13T18:00:00+05:30`). At writing it was 14:22 IST (about 3h38m left). Check the real clock with `date -Iseconds` at session start and before every log entry.
- Planned timeline (set by the orchestrator): code freeze **14:45 IST**, packaging (Phase 4B) 14:45–15:30 in a separate packaging chat, upload around 15:30, interview prep afterwards, buffer to 18:00.
- This file is **not** part of the submission. Do not commit it and do not put it in `code.zip`.

A second, orchestrator-oriented handover exists (useful background, same facts from the reviewer's point of view):
`/private/tmp/claude-501/-Users-aryamanjaiswal-Downloads-Github-pulls-hackerrank-orchestrate-september26/901d697d-ff9d-4c34-ae63-9a20827f79d9/scratchpad/HANDOVER_ORCHESTRATOR.md` (scratch; may disappear).

---

## 0. READ FIRST — current status in ten lines

1. HEAD is `346fb1a` ("feat: propose permitted spending changes when no unchanged plan meets the deadline"). Everything up to Phase 3D.5 is committed.
2. **Phase 3D.6 is implemented but NOT committed.** Uncommitted edits: `code/cashflow.py`, `code/engine.py`, `code/main.py`, `tests/test_cashflow.py`, `tests/test_income_facts.py`, `tests/test_spending_changes.py` (70 insertions, 12 deletions). `output.csv` at the repo root is untracked and currently holds the 3D.6 build (md5 `a50362440f09240bcc3e61f2a9742f98`).
3. 3D.6 = safety window `HORIZON_DAYS = 86` (was 90) + "lapsed income" rule (a recurring credit series that already missed its next expected payment is not projected).
4. Verified 3D.6 state (bytecode cleared): **378 tests OK**, contract clean, public samples **4/22/23/23/20/22** (safe/status/method/plan/earliest/spending), evaluation runner OK.
5. The orchestrator reviewed 3D.6 at 14:17 IST: **keep the 86-day window; the lapse rule needs a "successor exemption" fix** (details and the measured patch in §8). This fix is the most likely next task you will receive.
6. The human pastes task prompts from the orchestrator chat. Prompts start with `Chat: Same` or `Chat: New`. You implement, verify, and return a structured report. **You never commit**; the human commits by file name.
7. Always clear bytecode before verifying: `find code tests -name __pycache__ -type d -prune -exec rm -rf {} +` (a stale `.pyc` once served old code; §12).
8. `log.txt` is the scored chat transcript. Append a factual entry after every turn with real timestamps and `tool=claude-code` (§2).
9. Do not edit `dataset/`, `CLAUDE.md`, `AGENTS.md`, `code/README.md`, `code/evaluation/*` (packaging chat owns README/evaluation), and never hardcode request/user/event/message IDs in code or code comments.
10. Scoring is not only the CSV: output 30, code zip 30, AI judge interview 30, transcript 10 (§3).

---

## 1. Your role and how the collaboration works

- **Human (participant/operator).** Runs the chats, pastes prompts and reports between them, commits with git. Wants short, precise, honest answers; dislikes padding and yes-man answers. When they ask "is it better or worse", give a hard answer with numbers first. They sometimes mix up which chat did what, so verify repository state (`git status --short`, `git log --oneline -5`, `git diff --stat`) rather than trusting memory.
- **Orchestrator chat (Claude Code, separate).** Principal architect and reviewer. Writes one focused builder prompt at a time, re-runs verification on your diff, decides commit / focused fix / drop, and writes the commit commands. It often gives you a pre-measured grid and a bar ("adopt only if …, tie → simpler/conservative"). Implement what it specifies; do not re-litigate measured decisions, but do flag real problems with evidence.
- **Packaging chat (Claude Code, separate).** Owns `code/README.md`, `code/evaluation/main.py`, `code/evaluation/usage_report.md`, deleting `tests/calibration_report.py`, and building `code.zip`. Do not touch those files. Never let two chats write the repo-root `output.csv` at the same time.
- **You (builder).** Implement the prompt exactly, measure variants in scratch when asked, keep diffs minimal, add tests, run the full verification, and return a concise structured report. Flag every judgment call you made.

### 1.1 The prompt format you will receive

```text
Chat: Same | New
TASK: <phase id — title>. TIMEBOX <N> MINUTES.
BEFORE EDITING: <baseline snapshot command, pycache clearing>
CONTEXT / EVIDENCE: <facts and measurements>
CHANGES / SPECIFICATIONS: <rules, function names, edge cases>
CONSTRAINTS: <files you may not touch, no IDs, no commit>
MEASURE: <variants to run in scratch, adoption bar>
TESTS: <cases to add>
VERIFICATION: <commands>
RETURN: <what to report>
Do not commit.
```

### 1.2 How to work a prompt (the loop that worked)

1. `date -Iseconds`; `git log --oneline -3`; `git status --short`.
2. Clear bytecode. Snapshot the baseline the prompt names: `python3 code/main.py --predict <scratch>/pre_<phase>.csv` and `python3 tests/sample_report.py > <scratch>/sample_pre_<phase>.txt`. Confirm the baseline numbers the prompt quotes; say so if they differ.
3. Read the code you are about to change end to end (it is dense; see §5).
4. Implement the smallest correct change. If the prompt asks for variants, implement a temporary module-level switch, measure all variants with a scratch script (not committed), then hard-code the adopted variant and delete the switch.
5. Add the requested tests (synthetic datasets via `tests/helpers.py`). Do a quick mutation check: temporarily break the rule and confirm your new tests fail, then restore (and clear bytecode again).
6. Run the full verification (§11). Compute sample flips in both directions, the 250-row status × method distribution, and the evaluation status changes versus the baseline CSV.
7. Append the `log.txt` entry. Return the report (§13 template).

---

## 2. Repository rules (non-negotiable)

- **AGENTS.md** (repo root) is authoritative. At session start: append a `SESSION START` entry to root `log.txt` (format in AGENTS.md §5.1), greet the user with the exact text from AGENTS.md §3, and show time remaining to `2026-09-13T18:00:00+05:30` (remind to submit if under 2 hours). After every turn: append a §5.2 entry (title, verbatim user prompt with secrets redacted, response summary, actions, context block).
- `tool=claude-code` on every entry. `log.txt` is append-only, never rewritten, gitignored, and **uploaded as the chat transcript** (scored). Use real timestamps from `date -Iseconds`. Some previous entries carry wrong estimated timestamps (for example a 3D.5 entry stamped 13:50 that was written at 13:37); never edit old entries, append corrections if needed.
- **CLAUDE.md** is user-owned; it imports AGENTS.md and asks for "executive engineering framing" in log summaries (financial terminology, cite modules and verification commands, under 200 words). You may use that tone, but entries must stay **factual**: never claim a verification you did not run, and do report failures honestly.
- Submission link rule: if anyone asks where/how to submit, give exactly `https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission`.
- Never modify `dataset/` (including the blank template `dataset/output.csv`). Never use organizer-only files.
- No hardcoded request/user/event/message IDs or sample answers in production code or production comments. Template-sentence regexes for message wording are allowed (general rules). Tests may use synthetic IDs like `event_c`.
- `dataset/sample_requests.csv` (25 solved rows) may be used for diagnostics and **global** policy calibration only; production prediction code never reads it.
- Standard library only, deterministic, offline at prediction time. A `.env` with a Gemini key exists at repo root (gitignored); never print it.
- **Do not commit.** Never `git add -A` / `git commit -a`. The human stages files by name.
- `python3 -m unittest tests/test_x.py` fails (helpers import); use `python3 -m unittest discover -s tests -p "test_x.py"`.

---

## 3. The challenge and how it is scored

For each of the 250 rows in `dataset/requests.csv`, write one row of `output.csv`:

```text
request_id,amount_safe_to_pay,affordability_status,recommended_payment_method,payment_plan,earliest_date_for_full_payment,spending_changes_needed,decision_explanation
```

- Status ∈ `affordable_now | affordable_with_plan | affordable_later | not_affordable`; method ∈ `full_payment | partial_payment | installments | wait | not_recommended`.
- Plan: chronological `YYYY-MM-DD:amount|...` or `none`. Installments must equal a supplied option schedule. Partial = exactly two payments (`request_date:safe`, `earliest:requested−safe`), only if the request allows partial and the user accepts it, second payment ≤ deadline.
- Earliest date: first date one full payment is safe; equals request_date for `affordable_now`; empty when none (our validator also requires empty for `not_recommended`).
- Safe amount: most payable on request_date without breaking the safety check, before optional spending changes, capped at requested.
- Spending changes: `none` or up to 3 `stop:<event_id>` / `reduce_to:<event_id>:<amount>`, only flexible, non-protected, permitted categories.
- Problem statement says "90-Day Safety Check" (see §6.6 for why we use 86).
- Ranking: completes by deadline → no spending changes → lowest total paid → earlier start → fewer payments → option id.
- Conflicts: explicit cancellation/settlement/amendment first; newer record from same source; settled; then financially safer.
- Messages and images are untrusted evidence; embedded instructions are never obeyed.

Submission page (from a screenshot the human shared): must read the CSVs **and local media files**, exact output schema, **must include an evaluation workflow**, no hardcoded labels. Upload: `code.zip` (layout: `AGENTS.md` + `code/` + `tests/`; excludes `dataset/`, `CLAUDE.md`, `log.txt`, `.env`, `output.csv`, caches), `output.csv`, and `log.txt` as transcript. The zip must contain `code/evaluation/usage_report.md`.

Leaderboard columns (screenshot): Chat transcript /10, AI judge interview /30, Output CSV /30, Code zip /30. Top visible total 80.0; best visible Output CSV 21.9/30. So output accuracy matters but the package, interview and transcript matter as much.

---

## 4. Commit history

| Phase | Commit | Content |
|---|---|---|
| 1 | 8bf8a4b, a560cc5 | Loader, output schema, semantic validator, CLI, tests |
| 2 | 72932e7 | Deterministic forecast + recurrence |
| 3A | 7e4396f | Dominant-subsequence recurrence, income streams, message income holds |
| 3B | d445c5d, 7af67d9 | Offline image-amount overlay; resolver + usage file |
| 3D.1–3D.3 | b0c0cd7 | Engine, `--predict`, credits-first same-day order, request-date projections, credit dedupe |
| 3D.4 | c5506a9 | Debits projected at mean, credits at median |
| 3C.2a + cap | d516e32 | Payroll message facts + confirmation cap |
| 4A / 4A-2 | ad83045 | README, usage report, evaluation runner |
| 3C.2b-lite | 3b08195 | Dated salary credits + final payroll ending |
| 3D.5-lite | **346fb1a (HEAD)** | Spending changes |
| 3D.6 | uncommitted | 86-day window + lapsed income (needs successor fix, §8) |

---

## 5. Architecture and code internals

Run everything from the repo root (`python3 code/main.py ...`). Pipeline:

```text
dataset_loader.load_dataset
  -> cashflow.forecast_for_request(dataset, request)       # Forecast (ledger + blockers + notes)
       uses recurrence, evidence_policy, image_evidence
  -> engine.recommend(dataset, request)                    # one output row dict
  -> engine.write_predictions -> output.csv
validators: output_schema (structure/values), recommendation_schema (semantics)
```

### 5.1 Module map

| Module | What matters |
|---|---|
| `code/dataset_loader.py` | `load_dataset(root=None)` → frozen `Dataset` with `Decimal` money. Records: `FinancialProfile` (tuples like `expense_categories_to_protect`, `payment_methods_user_will_consider`, `max_installment_months` may be None), `Request`, `PaymentOption` (`schedule()`), `FinancialEvent` (`amount` may be None = blank; `flexibility`, `minimum_allowed_amount`, properties `is_stoppable`, `is_reducible`), `Message` (`sent_at` datetime, `request_id`/`related_event_id` may be None), `ImageRef`. Indexes: `event_by_id`, `events_for(user)`, `messages_by_user`, `request_by_id`, `sample_request_by_id`, `profile_by_user`, `options_for(request_id)`, `exchange_rate(date, from, to)` (same currency → 1; never inverts/derives). |
| `code/recurrence.py` | Settled-history recurrence. `recurrence_patterns(events, as_of)` → dict keyed `(category, direction, stream)`; debits have stream `""` (grouped by category); income credits split by description, with a merged-category fallback (stream `""`) when no single stream reaches 3 occurrences. `RecurrencePattern`: `event_ids`, `dates`, `cadence_days`, `cadence_cycle`, `month_day`, `last_date`, `occurrences_between(start, end)` (inclusive). `series_amount`: debit mean, credit median (half-up to cents). `_anchored(origin, months, day)` monthly helper (imported by cashflow). `MIN_OCCURRENCES = 3`. |
| `code/evidence_policy.py` | (a) `income_holds`: two explicit "pending / not withdrawable" markers withhold a stream's projections (commission in T08, gig payouts T16). (b) Income facts: `IncomeFact(kind, message_id, sent_at, scope, stream_words, related_stream, spared_word, amount, currency, effective_date)`; `INCOME_TEMPLATES` = tuples `(kind, scope, stream_words, spared_word, regex)`; `parse_income_facts(text, message_id, sent_at)`; `income_facts(dataset, user_id, as_of, request)` applies applicability (same user, `sent_at.date() <= request_date`, `request_id` blank or equal); `fact_targets(fact, streams)` resolves target projected series (linked event description, else stream words; one match = target; several = `APPLY_TO_ALL_WHEN_AMBIGUOUS[kind]`: True → all, False → none; none → note). `DATED_CREDIT_KINDS = {"first_income", "income_resumes"}` are **not** in `APPLY_TO_ALL_WHEN_AMBIGUOUS`; cashflow filters them (and `unpriced_commitment`) out before `fact_targets`. |
| `code/image_evidence.py` | Validates `code/evidence/image_amounts.json` (event still blank, image linked in `images.csv`, **SHA-256 of `dataset/media/images/<id>.png` matches**, currency, positive decimal) and fills blank amounts on event copies. 15/16 resolved; image_04 stays blank. |
| `code/cashflow.py` | Forecast building (details §5.2). `HORIZON_DAYS = 86` (uncommitted; 90 at HEAD). `Forecast`: `start_date`, `end_date = start + horizon`, `opening_balance`, `minimum_balance_to_keep`, `entries` (`LedgerEntry(day, amount signed, source_kind "event"/"recurrence"/"message", source_id, category, rationale, balance_after)`), `blockers`, `notes`, `is_complete`, `trace()`. `same_day_order(day, amount)` → credits before debits. |
| `code/engine.py` | Decisions (details §5.3). |
| `code/output_schema.py` | Structural/value validation, `parse_payment_plan`, `parse_spending_changes`, `MAX_SPENDING_CHANGES = 3`, `NO_PAYMENT_PLAN`, `NO_SPENDING_CHANGES`, `REQUIRED_OUTPUT_COLUMNS`. |
| `code/recommendation_schema.py` | Semantic validation. `_validate_spending_change(action, event_id, new_amount, request, profile, dataset, add)` (reused by the engine for eligibility). `validate_recommendations(columns, rows, dataset, requests=...)`, `validate_predictions_file`. |
| `code/main.py` | CLI: `--validate` (default), `--dataset DIR`, `--check-output PATH`, `--forecast REQUEST_ID` (ledger trace: `!` blockers, `~` notes), `--predict [PATH]`, `--max-issues N`. |
| `code/evaluation/main.py` | Evaluation workflow (packaging chat owns it): predicts to a path, contract check, status × method distribution, per-field scores on the 25 samples (report only). |
| `code/evidence/resolve_images.py` | One-time offline image resolver (not used at prediction). |
| `tests/sample_report.py` | Diagnostic: per-field sample matches and a mismatch dump. Same counts as the evaluation runner. |
| `tests/calibration_report.py` | Stale diagnostic (superseded safe-amount shortcut), referenced nowhere; packaging chat deletes it. |

### 5.2 `cashflow.build_forecast` / `_recurrence_entries` — exact order of operations

`build_forecast(dataset, user_id, start_date, horizon_days, request, image_evidence)`:
1. `apply_image_evidence` fills blank amounts.
2. `_event_entries`: books supplied rows. Excluded statuses: failed/cancelled/unrealized. Settled on/before start is already in the balance (not booked). Pending/scheduled are booked no earlier than start. Credits count only if not pending, `event_type == income`, category ≠ `windfall`. Blank amount → blocker; missing FX rate on the booking date → blocker.
3. `income_holds(...)` and `income_facts(...)` from messages.
4. `_recurrence_entries(...)`:
   1. `unpriced_commitment` facts → note only.
   2. `_dated_salary_credits` (3C.2b): for each `first_income`/`income_resumes` fact with `start <= effective_date <= end`, book the stated amount (converted on each date; missing rate → blocker naming the message) on the date and **continue monthly** (`_anchored`, same day of month) to the horizon end; a date outside the window → note only. Also scheduled salary-category income rows whose **category has no projectable income pattern at all** continue monthly from the month after the row (the row itself is booked by `_event_entries`). Source ids `salary/credit/<message_id or event_id>`, `source_kind "message"`. These movements are added to the ledger and to the credit-suppression loop.
   3. `_facts_by_series`: remaining facts mapped to projected income series via `fact_targets` (newest fact per `(series, kind)` wins).
   4. `_final_payroll_ends(events)`: per category, the settled counted income credit whose description matches `\bfinal\b` (case-insensitive) with no later settled/scheduled counted income credit in that category.
   5. For each pattern (sorted):
      - Credit series: skip unless the last event counts as income. **(3D.6) Lapse check:** if `start > last_date + cadence_days + cadence_days // 2` → note "last occurrence <date> and the next expected one never arrived; income not projected" and skip. Then holds (`_hold_for`) → note and skip.
      - Amount per currency (`_amount_by_currency` with `series_amount`); blank amount in the series → blocker.
      - `window = max(1, cadence_days // 2)`; `days = pattern.occurrences_between(start, end)` (occurrences due on start are included).
      - Credit series: `_apply_income_facts` (below), then final-payroll ending drops days after the final payroll (note).
      - Debit series: suppressed where an explicit booked debit of the same group lies within the window.
   6. Credit suppression: each booked positive credit (and each dated salary credit), in date order, replaces the nearest projected same-category credit within that projection's window, one for one (note).
   7. Booking: overrides (message amounts) convert on their day (missing rate → blocker naming the message); normal projections use `_projected_amount` (per-currency amount converted on the day; missing rate → blocker).
   8. `_unprojected_income_notes`: repeated settled income with no projectable schedule gets a note.

`_apply_income_facts(facts, days, source_id, start, end, window, history)`:
- Date facts first: `income_ended` keeps only days before the message `sent_at`; `payday_moved` rebuilds days monthly from the stated date (same day of month).
- Amount facts, oldest first: `one_off_extra` → note only (never booked, no dated one-offs exist). `fx_salary_confirmed` → outside window → note; else replaces nearest projected day within the window and books the stated date. `FROM_DATE` scope (increase, base/regular/remaining confirmed) → days ≥ effective date (or start). `NEXT_ONLY` (next reduced, temporary pay) → first day only.
- **Confirmation cap:** kinds in `CONFIRMATION_KINDS` (`base_salary_confirmed`, `regular_salary_confirmed`, `remaining_salary_confirmed`, and `fx_salary_confirmed` only when it replaced a nearby payday) never raise a projected amount above the series' history estimate (`_history_above`: same currency compared directly, else both converted on the day). Capped days keep any existing override untouched; note "confirms CUR A, above settled history CUR B; history amount kept (safer)".

### 5.3 `engine.recommend`

1. Incomplete forecast (any blocker) → conservative row: safe `0`, `not_affordable`, `not_recommended`, plan `none`, earliest empty, explanation lists the first two blockers. (request_78 is the only incomplete evaluation forecast: no USD→INR rate on projected date 2025-10-06.)
2. `simulate(forecast, payments)`: merges payments as debits, sorts with `same_day_order`, walks every checkpoint from the opening balance; safe iff lowest ≥ floor. Payments after the horizon still apply.
3. `amount_safe_to_pay`: 0 if the baseline breaches; else probe paying `requested` on start: `lowest + requested − floor`, clamped to `[0, requested]`, rounded down to cents.
4. `earliest_full_payment_date`: first day in `[start, end]` where one full payment is safe.
5. `_candidates(forecast, request, profile, options, safe, earliest)`: full (accepts full_payment, earliest == start → affordable_now); wait (accepts full_payment, start < earliest ≤ deadline → affordable_later); partial (spec rule → affordable_with_plan); installments (accepted, `max_installment_months` set, `number_of_payments ≤ max`, last payment ≤ deadline → affordable_with_plan). Only simulated-safe plans survive. Rank key `(total_cost, first payment date, number of payments, option_id)`; full/wait/partial use the request's full_payment option id.
6. **Spending changes (3D.5)**, only when there is no candidate: `_plan_with_changes`:
   - `_spending_actions`: for each recurring **debit** pattern with projected entries (`source_kind == "recurrence"`, `source_id == "<category>/debit"`), cited event = `pattern.event_ids[-1]` (latest settled occurrence), home currency only (`ponytail:` comment). Try `reduce_to minimum_allowed_amount` first (only if `minimum_allowed_amount` < every projected amount), else `stop`; an action is permitted iff `_validate_spending_change` reports no issue (covers direction, status, recurrence, protected category, flexibility, willing-to-reduce/stop lists, minimum, reduction below the cited event amount). Saving = sum over projected occurrences in the window. Sort by saving desc, then event_id.
   - Greedy: add actions one at a time (max 3); after each, `_with_changes` rebuilds the ledger (reduced amounts or removed entries, recomputed balances) and `_candidates` runs on the changed forecast with safe/earliest recomputed on it, **methods limited to `full_payment` and `installments`** (`CHANGE_METHODS`; wait/partial are dated by the unchanged earliest date and would fail validation). First set with a candidate wins.
   - Output: status forced to `affordable_with_plan`; method/plan from the changed-forecast candidate; `amount_safe_to_pay` and `earliest_date_for_full_payment` stay from the **unchanged** forecast; spending text `stop:<id>` / `reduce_to:<id>:<money>` joined by `|`, sorted by event_id; explanation starts "Reduce X to CUR n and stop Y, then pay …".
7. `money`: whole amounts without decimals, else exactly 2 dp. Explanation wording for "never safe" uses `f"within the {HORIZON_DAYS}-day forecast"` (3D.6).

---

## 6. Adopted policies and the evidence (do not silently reverse)

### 6.1 Forecast basics (before this builder chat)
- Opening balance = `current_available_balance`; settled on/before request_date never rebooked.
- Pending/scheduled debits reserved; pending credits, windfalls, refunds, investment gains never counted.
- Blank amount is never zero → blocker → conservative row.
- Credits before debits on the same day (5 samples treat salary-day payments as safe; debits-first missed 19 evaluation deadlines by one day).
- Occurrences due on request_date are projected.
- Booked credit replaces the nearest same-category projection within half a cadence, one for one.
- Debit overlap: explicit debit within half a cadence suppresses that projection.
- 3D.4 amounts: debit mean, credit median (48-config grid; status+method+earliest sample matches 31 → 47, no field regressed).

### 6.2 Phase 3C.2a (message facts) + cap — commit d516e32
- Templates implemented (EN and ID where the dataset has both): T14 salary increase (9), T08 base salary confirmed (9), T05 next salary reduced (10, EN only), T07 temporary pay (10), T13 regular salary + undated one-off (8 + 8), T22 FX salary confirmed (7, including one "salary credit for 15 September 2026" variant), T09 payday moved (7), T17 seasonal contract ended (9), T24 employment ended (4), T20 household income ended + remaining salary (7 + 7). 95 facts from all 215 messages; each applied fact leaves a note citing the message.
- Ambiguous targets: increases/confirmations → none; reductions/temporary/payday moves/ended → all candidates.
- T20: the stream containing "salary" is spared when exactly one household stream contains it; "Second household income" stops.
- Confirmation cap evidence: settled "Base salary" is exactly 0.60 × the stated confirmed base for all 9 T08 users; "Primary household salary" is exactly 0.62 × the stated remaining salary for all 7 T20 users; applying the stated T08 amount contradicted sample request_11's example earliest date (2025-07-15). Explicit changes (T14, T05, T07) keep stated amounts.
- Instruction-like sentences ("income that has ended should be removed from future estimates", "use the revised date for anything you pay around payday") are never parsed.
- Samples moved 3/16/17/17/14/22 → 2/16/17/17/15/22.

### 6.3 Phase 3C.2b-lite — commit 3b08195
- T01 first salary (13 EN + 1 ID), T02 new employer (5 EN + 2 ID), T03 scheduled first salary (3 EN + 3 ID), T06 salary resumes (8 EN, no ID wording exists); childcare sentence = `unpriced_commitment` note (8). Exact wordings were regenerated from `dataset/messages.csv`.
- Variant grid (samples / evaluation status changes): (a) date only 2/17/18/18/16/22 / 2; (b) + monthly continuation of message salaries 2/17/18/18/16/22 / 17; **(c) adopted** = (b) + monthly continuation of scheduled salary rows 3/18/19/19/17/22 / 19 (gained sample 01 on all five fields). Bar: adopt (b)/(c) only if status+method+earliest beat (a) and safe does not drop.
- Judgment call: scheduled-row continuation only when the **category** has no projectable income pattern (the stream-level reading broke three existing ledger tests and changed nothing in samples/evaluation).
- Final payroll: 7 events (sample 05; evaluation 75, 111, 165, 246 also have T24 messages; 174 and 255 do not). Sample 05 gained status/method/plan/earliest.
- Orchestrator evidence for continuing message salaries: 12 of 15 flipped users already had 2–3 settled salary credits of exactly the stated amount on the same monthly day.

### 6.4 Phase 3D.5-lite (spending changes) — commit 346fb1a
- Rules in §5.3 step 6. Evidence from samples 06, 11, 21: cited event is the latest settled occurrence; reduce uses `minimum_allowed_amount`; streaming was reduced (not stopped) when both were allowed; safe/earliest are computed without changes.
- Variant "stop preferred" tied on samples (3/20/21/21/17/21) and differed only on evaluation rows 58, 117, 135, 150, 165 → reduce preferred kept.
- Samples 3/18/19/19/17/22 → 3/20/21/21/17/21 (11 and 12 gained status/method/plan; 12 lost spending because the example needs no change there). 35 evaluation rows moved not_affordable → affordable_with_plan (12 full payment, 23 installments). An orchestrator audit found all 47 actions valid.
- Why 06/21 still miss: sample 06 our safe 523.82 vs example 603.3 (stop streaming only reaches 542.82 vs 620.40 requested); sample 21 we already say affordable_now (safe 1574.40 vs 1543.35) so no change is triggered; sample 11 needs three changes in ours (dining alone reaches 13,003,939.17 vs 13,110,000) because our unchanged safe amount is ~190k lower.

### 6.5 Phase 3D.6 (uncommitted) — 86-day window + lapsed income
- Orchestrator grid (current code): 90d 3/20/21/21/17/21; 76–86d 4/21/22/22/19/22 (gains 08 status/method/plan/earliest, 12 safe/earliest/spending); 87d 3/21/22/22/18/21; 88–89d = 90d; 60d worse. With lapse at 86d: 4/22/23/23/20/22 (13 gains status/method/plan/earliest). Samples 08, 12, 13 have commitments on days 87–90 that flip the reference decision; no sample's example earliest date or final plan payment is beyond day 76. 86 = largest value on the plateau, closest to the stated 90.
- A per-series cap anchored at each series' last occurrence was **rejected** (losses on samples 03, 16, 18, 22, 23). Do not implement it.
- My decomposition (scratch, bytecode cleared, total matched sample fields of 150):

| Window | Lapse rule | Samples | Total | Eval status changes vs pre-3D.6 |
|---|---|---|---|---|
| 90 | off (HEAD) | 3/20/21/21/17/21 | 103 | 0 |
| 90 | on | 3/20/21/21/17/21 | 103 | 5 (all less affordable: 97, 124, 126, 196, 223) |
| 86 | off | 4/21/22/22/19/22 | 110 | 15 (14 more affordable, 1 lateral) |
| 86 | on (current tree) | 4/22/23/23/20/22 | 114 | 20 (14 more, 5 less, 1 lateral) |

- Risk acknowledged by everyone: `problem_statement.md` literally says "90-Day Safety Check"; 86 is sample-calibrated. The orchestrator approved keeping 86.

### 6.6 Current 3D.6 build numbers (uncommitted tree, verified)
- 378 tests OK. `--check-output` clean. Evaluation runner OK.
- Samples 4/22/23/23/20/22. Flips vs 3D.5: gained 08 (status, method, plan, earliest), 12 (safe, earliest, spending), 13 (status, method, plan, earliest); lost none.
- Status × method (pre-3D.6 → now): affordable_later×wait 52 → 59; affordable_now×full_payment 55 → 58; affordable_with_plan×full_payment 12 → 8; affordable_with_plan×installments 60 → 60; affordable_with_plan×partial_payment 9 → 9; not_affordable×not_recommended 62 → 56. Rows with spending changes 35 → 30.
- Evaluation status changes vs `pre_3d6.csv` (20): to affordable_later 32, 33, 37, 40, 104, 232 (from not_affordable) and 42 (from affordable_with_plan); to affordable_now 123, 255, 265 (from not_affordable) and 165, 201, 237 (from affordable_with_plan); to affordable_with_plan 75, 215 (from not_affordable); to not_affordable 97, 124 (from affordable_with_plan) and 126, 196, 223 (from affordable_now).
- Lapse note present on 42 evaluation forecasts and 5 samples (05, 10, 11, 12, 13).
- Tests updated in 3D.6 (and why): `test_cashflow.LedgerBoundaryTests.test_each_booked_credit_suppresses_at_most_one_occurrence` (2026-04-05 is past the window ending 2026-04-01); `test_income_facts.FinalPayrollTests` history (the old history's last payday 11-15 had already lapsed, hiding the final-payroll rule; now Payroll credit through 12-15, final 2026-01-02, later settled credit 2026-01-04); `test_spending_changes.test_at_most_three_changes` fallback amount 620 → 625 (music's 04-02 occurrence is outside the window, so three changes now reach 620). New: `test_income_facts.WindowAndLapseTests` (lapsed credit not projected + note; on-time credit projected; debits never lapse; window includes day 86 = 2026-04-01 and excludes day 87).

---

## 7. Remaining sample mismatches in the current tree (4/22/23/23/20/22)

Non-safe-amount mismatches:
- **06**: ours not_affordable/none; example affordable_with_plan, full_payment 2026-01-03:620.40, earliest 2026-01-15, `stop:event_476` (expense estimates ~80 EUR higher in ours).
- **11**: earliest ours 2025-06-15 vs 2025-07-15; spending ours 3 changes vs one reduce (safe ~190k lower in ours).
- **17**: earliest ours 2026-04-15 vs 2026-03-15.
- **19**: ours incomplete (image_04 cut off → blocker) vs example partial_payment. No evaluation row affected.
- **21**: ours affordable_now/none vs example affordable_with_plan with `stop:event_1815|reduce_to:event_1816:23.50` (forecast ~31 USD too rich).

Safe amount matches only 4/25 (01, 09, 12, 16). Deltas are mixed in sign (ours higher on 02, 03, 04, 05, 08 by 0.63, 10, 13, 14, 18, 20, 21, 23, 24; lower on 06, 07, 11, 15, 17, 22, 25; 19 is 0 because its forecast is incomplete). Sample 25 is far off (376,083.39 vs 1,425,000; weekly dining reserved twice before payday in ours). Not being worked on before the freeze.

---

## 8. The next task you will most likely receive: lapse "successor exemption"

Orchestrator verdict at 14:17 IST: keep `HORIZON_DAYS = 86`; fix the lapse rule, then commit 3D.6.

Problem found: the five rows that became not_affordable (97, 124, 126, 196, 223) each have a lapsed income stream that was **succeeded by a new settled stream in the same category**: "Previous employer payroll" → "New employer payroll" (plus a scheduled "Next confirmed salary") on 97, 124, 196, 223, and rotating freelance descriptions on 126. The rule wrongly treated a job change as lost income.

Patch the orchestrator measured in scratch (`.../901d697d-ff9d-4c34-ae63-9a20827f79d9/scratchpad/succ/cashflow.py`, line ~393), replacing the lapse condition:

```python
succeeded = any(
    e.category == category and e.description != sample.description and e.status == 'settled'
    and _counts_as_income(e) and e.settlement_date and pattern.last_date < e.settlement_date <= start
    and not any(o.description == e.description and o.status == 'settled' and o.settlement_date
                and o.settlement_date <= pattern.last_date for o in events)
    for e in events)
if not succeeded and start > pattern.last_date + timedelta(days=pattern.cadence_days + pattern.cadence_days // 2):
    ...lapse note; continue
```

Meaning: a lapsed credit series is still projected when, after its last occurrence and on/before the request date, a settled counted income credit in the same category arrived under a **different description that had never settled before** that last occurrence (a genuine successor stream).

Measured effect of that patch: samples unchanged at 4/22/23/23/20/22; evaluation status changes vs HEAD 20 → 15 (all more affordable, none less); lapse notes 42 → 21; only rows 97, 124, 126, 196, 223 differ from the current tree. The orchestrator's prompt will give the exact spec; implement it cleanly (readable names, a comment, no IDs), add tests (a lapsed "Previous employer payroll" with a newer settled "New employer payroll" in the same category is still projected; a lapsed stream with no successor is not; a successor whose description already settled before the lapse does not count), clear bytecode, run full verification against a fresh baseline, and return the flips. Expected commit afterwards (human runs it):

```text
git add code/cashflow.py code/engine.py code/main.py tests/test_cashflow.py tests/test_income_facts.py tests/test_spending_changes.py
git commit -m "feat: 86-day safety window and lapsed income streams"
```

After that: **code freeze** (no product logic changes). The packaging chat then updates `code/README.md` (window, lapse rule, spending changes now generated, request_78 limitation, calibration tables, sample scores), fixes usage report paths, deletes `tests/calibration_report.py`, rebuilds output and `code.zip`, and verifies from a clean unzip. You may be asked to help with verification only.

---

## 9. Explicitly deferred / not doing (unless the orchestrator says otherwise)

- request_78 FX fix (convert historical observations at their own dates; 1 row).
- T04 invoice partly approved holds (15 messages), T21 bonus-hold English marker (8 messages).
- T10 rent increase %, refunds, disputes, transfers and other expense-side templates.
- Safe-amount calibration (expense estimator differences on samples 06, 21, 25, …).
- Debit overlap under-reservation, image_04 blocker, earliest date for not_recommended rows.
- A dated one-off extra (T13) rule: no message states a date.

---

## 10. Tests: map and helpers

Counts per file (current tree, 378 total): test_cashflow 61, test_cli 9, test_dataset_loader 16, test_dataset_validation 25, test_engine 23, test_evidence_policy 23, test_image_evidence 13, test_income_facts 38, test_output_schema 40, test_parsing 13, test_recommendation_schema 46, test_recurrence 47, test_resolve_images 16, test_spending_changes 8.

- `tests/helpers.py`: `default_tables()` (one user `user_a`: ZAR, balance 1000, floor 200, protect rent|groceries, reduce dining|shopping, stop streaming|shopping, methods full_payment|installments, max 6; `request_a` 2026-01-05 amount 500 deadline 2026-02-05 partial true; events: rent one-off, blank utilities `event_b`, dining `event_c1..c3` + `event_c` every 21 days (110/95/105/100, reducible_or_stoppable, min 40), streaming `event_d1..d3` + `event_d` monthly on the 12th (20, stoppable), salary `event_e` single 2025-12-25, shopping one-off `event_f`; EUR→ZAR rate 2026-01-05; two options; message_a; image_a), `build_dataset(base, tables=overrides)`, `REAL_DATASET`, `OUTPUT_HEADER`.
- `tests/test_income_facts.py` helpers: `income(event_id, day, description, amount, currency)`, `series(prefix, description, days=HISTORY)` with `HISTORY = 2025-09-15..2025-12-15` monthly, `PROJECTED = 2026-01-15, 02-15, 03-15`, `notice(text, message_id="message_n", sent_at="2026-01-04T09:30:00Z", **overrides)`, `forecast(messages, incomes=None, rates=None)` (default debits without credits + given incomes), `credits(result, stream="")` (projected/dated salary credits by source id prefix `salary/credit`), `salary_credits(result)` (all salary-category credits incl. booked rows), `normal(*amounts, days=PROJECTED)`, `cited(result, message_id)`. Classes: AmountTemplateTests, FxSalaryTests, DateAndEndTemplateTests, ApplicabilityTests, DatedSalaryTests, FinalPayrollTests, WindowAndLapseTests, RealDatasetCoverageTests (exact fact counts per kind on the real messages: salary_increase 9, base_salary_confirmed 9, next_salary_reduced 10, temporary_pay 10, regular_salary_confirmed 8, one_off_extra 8, fx_salary_confirmed 7, payday_moved 7, income_ended 20, remaining_salary_confirmed 7, first_income 27, income_resumes 8, unpriced_commitment 8 — update it if you add templates).
- `tests/test_spending_changes.py`: `run(requested, extra_events=(), **profile_overrides)` recommends for `request_a` with deadline 2026-01-10, full payment only, partial false, and validates the row; baseline safe is 330. `monthly(prefix, category, amount, day)` builds three settled monthly stoppable debits.
- `tests/test_cashflow.py` `LedgerBoundaryTests` uses day-5 monthly series and scheduled "Next confirmed salary" rows; those tests constrain the credit suppression and scheduled-row behaviour.

---

## 11. Verification recipe and measurement techniques

```text
date -Iseconds
find code tests -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -m unittest discover -s tests -q
python3 code/main.py --predict && python3 code/main.py --check-output output.csv
python3 tests/sample_report.py                     # header = per-field counts; then mismatch dump
python3 code/evaluation/main.py <scratch>/eval.csv # contract + distribution + sample scores
git diff --check -- code tests
grep -nE "request_[0-9]|user_[0-9]|event_[0-9]|message_[0-9]" code/*.py   # only hit allowed: the CLI usage example "--forecast request_26" in the code/main.py docstring
md5 -q output.csv; PYTHONHASHSEED=1 python3 code/main.py --predict <scratch>/rerun.csv; md5 -q <scratch>/rerun.csv  # must match
python3 code/main.py --forecast request_13          # trace one request
```

Measurement tips that worked:
- **Horizon without editing code:** `cashflow.forecast_for_request.__kwdefaults__["horizon_days"] = N` in a scratch script (the engine imports that same function object).
- **Rule on/off:** copy the **whole** `code/` directory (`cp -R code <scratch>/variant`), `sed` the rule off, and put that copy first on `sys.path`. Copying only `code/*.py` silently drops `code/evidence/image_amounts.json`, so blank amounts stay unresolved and every number is wrong (this happened once: 98 instead of 103).
- **Variant grids:** a scratch script that loads the dataset once, sets a module-level switch (for example `engine.PREFER_REDUCE`), computes per-field sample matches with `sample_report._same`, and counts evaluation status changes against a baseline CSV. Remove the switch before finishing.
- **Sample flips:** parse two `sample_report.py` outputs (lines `request_XX` then indented `field ours=... example=...`) and diff the wrong-field sets per request.
- **Evaluation diffs:** `csv.DictReader` both files, compare `affordability_status` and the other fields; list request ids in both directions.
- **Mutation check:** `sed` the rule off, run the new test file with `discover -p`, restore the file from a backup copy, clear bytecode.

Baseline files in my scratchpad (may disappear): `/private/tmp/claude-501/-Users-aryamanjaiswal-Downloads-Github-pulls-hackerrank-orchestrate-september26/512b6278-6add-4183-b8b4-2334c161669d/scratchpad/` — `pre_3c2b.csv`, `pre_3d5.csv`, `pre_3d6.csv` (HEAD 346fb1a output), `sample_pre_3d6.txt`, `sample_3d6.txt`, `d_90_nolapse.csv`, `d_86_nolapse.csv`, `d_90_lapse.csv`, `d_86_lapse.csv`, `decomp.py`, `grid.py`, `grid3d5.py`, `nolapse/` (code copy with lapse disabled). Orchestrator scratch: `.../901d697d-ff9d-4c34-ae63-9a20827f79d9/scratchpad/` — `head_output.csv` (HEAD output, md5 `d2de22880df192d535b0929a520cfde0`), `succ/` (successor patch copy), `succ.csv`, `lapse_audit.py`, `score.py`, `grid.py`, `diffs.py`.

---

## 12. Hazards and lessons

1. **Stale bytecode.** A temporary mutation's `.pyc` (same size, same second as the restore) was reused by Python, so a test failed and `output.csv` carried the wrong variant. Clear `__pycache__` before every verification, after every mutation restore, and before any build.
2. **Whole-directory copies** for variants (see §11).
3. **Real timestamps** only (`date -Iseconds`); estimated timestamps once caused a multi-hour misjudgment of remaining time.
4. **Reports vs reality.** Check `git status`, `git log` and `grep "^## \[" log.txt | tail` instead of trusting what anyone "remembers".
5. **Parallel chats.** Disjoint files only; do not run `--predict` into repo-root `output.csv` while another chat is building the final output.
6. **Private helper reuse.** `engine.py` imports `recommendation_schema._validate_spending_change` and `cashflow.py` imports `recurrence._anchored`; keep those names stable.
7. **Test constraints.** Existing ledger tests encode one-for-one credit suppression and scheduled-row behaviour; if your change breaks them, prefer a narrower reading of the spec and report it rather than rewriting those tests.
8. **Never rewrite log entries**; append corrections.

---

## 13. Report template to return (concise)

```text
1. Files changed; test count (before → after); updated tests, each with its reason.
2. Measurements (if asked): variant table with per-field sample scores and evaluation status-change counts; adopted variant and why (the bar).
3. Samples before → after (safe/status/method/plan/earliest/spending), flips in BOTH directions with a traced cause for any wrong flip.
4. 250-row status × method distribution (before → after) and the evaluation rows whose status changed vs the named baseline CSV (listed, grouped by direction).
5. Judgment calls and anything that differs from the expected numbers, with the cause.
6. Verification commands run and their results (tests, --check-output, sample_report, evaluation runner, diff --check).
Not committed.
```
