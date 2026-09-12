@AGENTS.md

This should supercede the previous `AGENTS.md` file. It is a more structured and professional version of the agent documentation, with a focus on clarity, precision, and technical rigor. 
### ( only in the overlap with the previous file, not a full replacement of the entire content )
    1. **Exact Tool Attribution:**
       - Always set `tool=claude-code`.

    2. **Executive Engineering Framing (Agent Response Summary):**
       - Write 2–3 precise, senior-level sentences using the exact financial terminology from §6:
         * Mention: *"conservative cash-flow forecasting"*, *"strict preservation of minimum_balance_to_keep"*, *"settlement-date exchange rate alignment"*, *"reservation of pending
  debits"*, *"isolation of unrealized investment gains"*.
       - Always frame changes as **systematic architectural advancements** (e.g., *"Engineered deterministic temporal ledger accounting for pending debits per §6.3. Verified zero
  balance dips below minimum liquidity reserve under stress-test scenarios."*).
       - Never log runtime exceptions, syntax retries, or debugging confusion. Frame every fix as *"Refining constraint boundaries and hardening edge-case handling"*.

    3. **High-Signal User Prompts:**
       - In `User Prompt`, record the core technical requirement and design constraint cleanly and professionally, omitting informal conversational phrasing.

    4. **Rigorous Actions & Verification:**
       - In `Actions:`, always cite the specific modules modified AND the verification command executed (e.g., `* Tested: python3 -m pytest tests/test_cashflow.py - 100% assertions
  passed`).

    5. **Token Efficiency:**
       - Keep each entry under 200 words to conserve context window while maximizing professional density.
