---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
title: CSV Import Adapters for Non-FinTS Accounts - Plan
type: feat
date: 2026-09-03
origin: docs/plans/2026-07-02-001-feat-product-iterations-plan.md
---

# CSV Import Adapters for Non-FinTS Accounts - Plan

## Goal Capsule

**Objective:** A person can bring an account Mentat cannot reach over FinTS — a broker, an app bank, a crypto exchange — into the same local ledger as their bank accounts, and can keep importing that account's exports for months without the ledger's history becoming wrong.

**Means:** A generic CSV importer driven by a local JSON mapping profile that declares the file's shape rather than guessing it (KTD1).

**Authority hierarchy:** This plan governs U3. Where it conflicts with the U3 section of `docs/plans/2026-07-02-001-feat-product-iterations-plan.md`, this plan wins — that section is a planning-time prediction, and its `Files:` list names paths that do not exist. Where it conflicts with the merged ledger contract in `src/personal_finance_agent/ledger/`, the code wins: this plan changes no ledger code (KTD4).

**Stop conditions:**

- Stop and ask before adding any runtime dependency. The project is standard-library-only and `pyproject.toml` declares no runtime dependencies.
- Stop and ask before changing the identity formula of any existing record type, or before modifying anything under `src/personal_finance_agent/ledger/`. This plan is a caller of that package, not a change to it.
- Stop and ask before relaxing the runtime path boundary or the private-permission behaviour from U1, and before introducing a second path-boundary concept for this one command.

## Product Contract

### Summary

Add a `ledger import-csv` command that reads a delimited export, maps its columns onto ledger transaction records through a user-editable JSON profile, and writes them into the existing local ledger. The profile declares the file's shape — encoding, delimiter, decimal mark, date format, and how the source encodes the sign of an amount — plus the account and the period the export covers. Re-importing an overlapping export corrects that period instead of failing or deleting history.

### Problem Frame

Mentat can reach German FinTS banks through AqBanking, and since U2 it has a local ledger to put that data in. It cannot reach anything else. Brokers, app banks, and crypto exchanges in the DACH region either have no API for individuals or gate it behind business accounts, and a CSV export is the only route out. Without that route the ledger holds one slice of a person's finances, and the overview it produces is confidently incomplete — which is worse than obviously incomplete, because nothing signals the gap.

The exports themselves are hostile to a naive reader. No provider publishes a format specification. Delimiters are semicolons because decimal commas make commas unusable. Encodings contradict each other across providers and sometimes within one provider's history. ING writes metadata lines and a blank line before the header row, and ships two column-count variants of the same account type. comdirect changed its export format at one point and broke downstream parsers with no announcement. The sign of an amount is expressed in at least four different shapes across the ecosystem.

### Key Decisions

- **The profile declares the file's shape; the importer never infers it.** Chosen over sniffing after finding that hledger and GnuCash both converged on explicit declaration, and that provider encodings are contradictory enough that inference would be a coin flip. Governs R1, R2.
- **An import is scoped to an account and a declared period, not to a file.** Chosen over one-file-one-slice after reproducing the failure that causes, and over deriving the period from the imported rows after finding that derivation loses records at the period's edges. See KTD4 and KTD7. Governs R5, R6, R14.
- **The importer refuses a partial parse, and there is no opt-out.** Chosen over a skip-invalid-rows escape hatch after verifying that a skip both deletes history on re-import and shifts the ordinal tiebreaker, minting parallel identities that no later import can reconcile. Governs R7.
- **U3 changes no ledger code.** Chosen over amending `replace_transaction_slice` after verifying that the caller can achieve period-scoped supersession with the merged API unchanged. See KTD4. Governs R5.
- **U3 carries no categorisation.** Merchant normalisation, tags, and review queues belong to U4, which depends on U3. Anything U3 writes into `purpose` becomes permanent transaction identity, so a merchant-cleaning rule here would be one U4 could never revise. Governs R11.

### Requirements

- **R1. Declared file shape.** The profile must declare encoding, delimiter, quote character, decimal mark, thousands separator, and date format. The importer must not guess any of them.
- **R2. Declared amount encoding.** The profile must support the four shapes a source uses for sign: one signed column, one column whose sign must be inverted, separate debit and credit columns, and one amount column plus a separate debit/credit indicator column.
- **R3. Header located by name.** The importer must find the header row by matching the column names the profile declares, tolerating leading metadata lines and a BOM. It must not rely on a fixed line offset or on column position.
- **R4. Stable account identity.** The profile must declare the account it imports into and the kind of identifier that is (IBAN or opaque), so repeated imports produce the same ledger account. The importer must write an `AccountRecord` for it.
- **R5. Period-scoped supersession.** A re-import must correct the transactions inside the declared period and must not affect transactions outside it.
- **R6. Overlapping re-imports.** Importing an export whose period overlaps a previously imported one must succeed.
- **R7. All-or-nothing rows.** If any data row fails to parse or validate, the import must write nothing and must report every failing row.
- **R8. Value-free diagnostics.** Diagnostics must identify the failing field, the profile key, the line number, and the column, and must not echo cell contents. This binds `provenance.source_ref` and every diagnostic the ledger emits on this path, not only the importer's own.
- **R9. Refuse unsafe inputs.** Compared on fully resolved paths, the importer must refuse the ledger, its lock file, the balances CSV, any file Mentat exported, anything that is not a regular file, and anything that is not text.
- **R10. Inputs are read-only.** The importer must not modify the source file's contents, permissions, or location.
- **R11. No classification.** The importer must not derive categories, tags, or merchant names. It may compose `purpose` by concatenating declared source columns in a declared order — mechanical assembly, not interpretation.
- **R12. Local profiles.** A mapping profile must live at a path the repository ignores by default, must never need committing, and must be created with the private permissions U1 established for runtime files.
- **R13. Documented.** `docs/usage/csv-imports.md` must let a user write a profile for their own export without reading Python.
- **R14. Declared period.** The profile or the command must declare the period the export covers. The importer must not derive it from the imported rows.
- **R15. Preview before write.** The command must offer a mode that runs the full parse and normalisation, reports what it would write, and exits without opening the ledger.

### Acceptance Examples

- **AE1. First import.** A profile declares a semicolon-delimited, ISO-8859-1 export with `DD.MM.YYYY` dates and a decimal comma. `ledger import-csv` writes its rows as ledger transactions with provenance, writes the account record, and exits 0.
- **AE2. Exact re-import.** Running the same import again reports duplicates, changes no record, and leaves the ledger file byte-identical.
- **AE3. Overlapping re-import.** An export declared for January to June is imported, then one declared for March to September. The second import succeeds. January and February transactions remain. March to June transactions are superseded. Covers R5, R6, R14.
- **AE4. Corrected export.** An export is re-downloaded after the provider corrects one booking's purpose text. Re-importing writes the corrected record and retires the superseded one. Because `purpose` enters transaction identity, the store reports this as one addition and one retirement — not as an update — and the summary must say so.
- **AE5. Malformed row.** One row in a thousand has an unparseable date. The import writes nothing, exits 2, and reports the line number, the field, and the date formats it tried, without printing the cell.
- **AE6. Wrong profile.** The profile names a column the file does not contain. The import fails before reading any data row and names the profile key and the missing column name.
- **AE7. Coexistence.** After importing a broker export, `ledger inspect --json` returns both the imported transactions and the FinTS balances, and neither is affected by the other. Covers R11.
- **AE8. Preview.** The preview mode renders the mapped rows and the declared period and exits 0 without opening the ledger. Covers R15.

### Scope Boundaries

**In scope:** the import command, the profile schema and its validation, the DACH format handling in R1 and R2, period-scoped supersession from the caller side, refusal of unsafe inputs, the preview mode, and the usage documentation.

**Deferred to follow-up work:**

- Joining a CSV-imported account to the same ledger account as its FinTS balances. See Open Questions.
- A `--skip-invalid-rows` mode. Four independent reviews found it actively harmful in its planned form; if a permanently unparseable row appears in practice, the remedy is to correct a copy of the export, and the mode can be reconsidered with the ordinal problem solved.
- A profile scaffolding helper that inspects an export and emits a draft profile. See Open Questions.
- Provider-specific profiles shipped in the repository. A shipped profile is a maintenance promise against a format that changes without announcement.
- Exporting transactions back to CSV. `export.py` only exports balances, so imported transactions are readable today through `ledger inspect --json`.
- Holdings and positions. Broker exports commonly carry them; `HoldingRecord` exists but no live source writes it.

**Out of scope:** live broker or exchange APIs, PDF and XLSX import, tagging and categorisation (U4), and any change to how balances are identified.

### Success Criteria

- A person who has never read this codebase can import their own broker export by copying the documented example profile and editing the column names, the account identifier, the period, and the format declarations.
- Re-importing an overlapping export leaves the transaction count for the imported account equal to the count in the provider's own statement for that period.

### Open Questions

- **Deferred — the FinTS account join.** `_account_id` derives account identity from `provenance.source` and `source_account_id` (`src/personal_finance_agent/ledger/models.py:506`). The balance path hardcodes `source="AqBanking"` (`src/personal_finance_agent/aqbanking.py:267`). Verified: for the same IBAN, `account_record(iban, Provenance("AqBanking", ...))` matches the balance record's `account_id`, and `Provenance("ING", ...)` does not. This plan takes the honest label and accepts two account records for one real account. The cost of closing it later grows with every import, because transaction identity embeds the source label — so U4 and U6 must not assume one ledger account per real account. Not blocking for U3: AE7 requires coexistence, not a join.
- **Deferred — profile authoring cost.** R1 requires the user to declare six format properties, and encoding is not something a person can read off their own file. A scaffolding helper that inspects an export and emits a draft profile would remove the discovery burden without weakening KTD1, since nothing would be inferred at import time. Not in U3's scope; revisit after the first real profile is written by hand.

## Planning Contract

### Key Technical Decisions

**KTD1. The mapping profile is JSON, and it declares rather than infers.** `tomllib` arrived in Python 3.11. `pyproject.toml` declares `>=3.11` and CI runs 3.12, but the development machine runs 3.9.6 and the suite passes there; nothing in `src/` uses a 3.10+ runtime feature, and `models.py:587` hand-rolls the `Z`-suffix handling 3.11 made native. TOML would be the repository's first hard 3.11 dependency and would break the local test loop. JSON costs nothing and matches how every other artifact in the repo is serialised. Governs R1, R12.

**KTD2. Amount encoding is a strategy the profile selects, not a column it names.** Sources express sign in four shapes, and a profile that only names an amount column cannot read the other three. Firefly III arrived at the same four-way taxonomy after years of production exposure. Governs R2.

**KTD3. The header row is found by matching declared names; column position is never load-bearing.** ING is confirmed to write metadata lines and a blank line before the header, and to ship 7-column and 9-column variants of the same account type. A fixed skip count breaks on the variant; a positional mapping misreads it silently, which is worse. Governs R3.

**KTD4. Period-scoped supersession is achieved from the caller. No ledger code changes.** A slice is keyed on `(provenance.source, provenance.source_ref)`, and `replace_transaction_slice` refuses an incoming identity that already exists under a different slice (`src/personal_finance_agent/ledger/store.py:165-178`). Two facts follow. First, `source_ref` must not derive from the filename: real bank filenames embed the download date, so every re-download would be a new slice, and the overlapping-range import fails outright — reproduced. Second, with a stable `source_ref`, `replace_transaction_slice` retires everything in the slice absent from the incoming set, which would delete the months outside a narrower export.

The resolution needs no amendment: the importer reads the slice's existing records, keeps those whose booking date falls outside the declared period, and passes them to `replace_transaction_slice` together with the parsed rows. Verified end to end — a January–June import followed by a March–September import preserves January, retires a superseded April record and a withdrawn May record, and reports `added=1, removed=2`. Governs R5, R6.

**KTD5. A partial parse is fatal, with no opt-out.** Verified: with `complete_source_slice=True`, re-importing a slice with one row skipped retires the skipped record — `removed=1`, and the command reports success. A skip therefore deletes history. Worse, `transaction_records` assigns the ordinal tiebreaker by counting occurrences within the rows it is handed (`models.py:264,278-280`), so skipping a row that belongs to a group of content-identical payments renumbers the survivors and produces different identities than a later complete import — permanent duplicates with no migration hook. The planned escape hatch made both failures reachable, so it is deferred rather than built. Governs R7.

**KTD6. Diagnostics name classes and positions, never values.** Issue #3 asks for actionable mapping guidance; KTD6 of `docs/plans/2026-07-02-002-fix-local-banking-security-boundaries-plan.md` forbids error messages from echoing parsed content, and cells in these files hold IBANs, holder names, and amounts. The resolvable form is: field name, profile key path, 1-based line and column, the formats attempted, counts and ratios, and character-class descriptions. Three extensions of that rule are load-bearing here:

- A header name may be quoted only from a row confirmed to be the header row, and only after control characters and escape sequences are stripped and the value is truncated — providers put account metadata in the preamble, and a wrongly detected header row would echo transaction data.
- When no header row matches at all, the diagnostic reports each candidate row's column count and character classes, never its contents.
- `provenance.source_ref` is persisted into every record and is echoed by the ledger's own merge diagnostics (`store.py:215-233`). It is therefore covered by this rule; see KTD10.

Governs R8.

**KTD7. The covering period is declared, never derived.** Deriving it from the parsed rows fails in four ways, each verified against the store's behaviour or reasoned from the export shapes in Sources: a non-contiguous export spans a gap and retires the months inside it; a backfilled transaction pulls the lower bound backwards and retires records before the export's real start; a withdrawn first or last booking shrinks the window inward so the withdrawn record can never be retired by any future import; and an export with zero data rows has no minimum or maximum at all. The profile declares the period, and the command may override it. An export with no data rows is refused unless a period is declared explicitly, in which case that period is retired. Governs R14.

**KTD8. Amounts are normalised to the shape the ledger already holds, and the shape does not assume fiat.** The ledger stores amounts as opaque strings and validates only non-emptiness, so `"1234,56"`, `"1234.56"` and `"+1234.56"` would all round-trip and all produce different identities. The normal form is: optional leading minus, digits, a dot, the source's own significant decimals, a space, an uppercase alphanumeric denomination code. Thousands separators are stripped before the decimal mark is converted — `value.replace(",", ".")` turns `1.234,56` into garbage, and here that garbage becomes permanent identity. The decimals are not fixed at two and the code is not restricted to ISO 4217: verified that the ledger accepts `0.00000001 BTC` and `12.5 ETH`, and the Objective names crypto exchanges, so a two-decimal fiat-only form would have excluded them for no reason the ledger imposes.

**KTD9. `source_account_id` normalisation is scoped by the declared identifier kind.** Verified: `"AT61 1904 3002 3457 3201"` and `"AT611904300234573201"` produce different account records, and exports print IBANs grouped while AqBanking emits them compact. For an identifier the profile declares as an IBAN, the importer strips whitespace and upper-cases. For an opaque identifier — a broker reference, a wallet address — it strips only surrounding whitespace and preserves case, because base58 and similar encodings are not recoverable after case folding.

**KTD10. `source_ref` is a profile-declared opaque label.** It is the slice key, it is written into every record, and the ledger echoes it in merge diagnostics. Deriving it from the input filename or path would persist an IBAN or account number — DACH export filenames routinely embed them — and print it to stderr. Deriving it from the profile's own filename would re-key the slice whenever the profile is renamed or moved, which fails the next import with the cross-slice error KTD4 exists to avoid. So the profile declares it explicitly, and the documentation states that changing it orphans existing history. Governs R8, R5.

**KTD11. The input path is read anywhere; the write boundary is untouched.** `--allow-outside-runtime` is a per-command flag applied to every path a command resolves — verified at `cli.py:94-100` and `cli.py:184-187`. Reusing it to accept an export from a downloads folder would simultaneously relax the boundary on the ledger write path for that invocation. The CSV input and the profile are read-only inputs and are accepted from anywhere without a flag, subject to R9's refusals; the ledger and any output path keep the existing `resolve_runtime_path` gate and its flag. Governs R9, R10, R12.

**KTD12. New modules stay 3.9-compatible.** Every module in `src/` opens with `from __future__ import annotations`, and the one runtime type expression avoids PEP 604 deliberately. CI runs 3.12 and will not catch a regression, so a new module using `match`, a runtime `X | Y`, or `dataclasses(slots=True)` would break the local loop silently. Every new file follows the existing convention.

### High-Level Technical Design

```
export.csv ──┐
             ├─→ profile validation ─→ header location ─→ row parse ─→ diagnostics
profile.json ─┘        (fatal)            (fatal)         (fatal per R7)      │
                                                                              ▼
                                                            all rows valid? ──no──→ exit 2, write nothing
                                                                    │yes
                                                                    ▼
                                                          --dry-run? ──yes──→ render, exit 0 (R15)
                                                                    │no
                                                                    ▼
                                      read declared period from profile/CLI (KTD7)
                                                                    ▼
                                      ledger.locked():
                                        session.add([account_record])
                                        keep := stored slice records outside the period
                                        replace_transaction_slice(keep + parsed, provenance)   (KTD4)
                                                                    ▼
                                                  summary to stdout, diagnostics to stderr
```

The importer owns its own row validation. `transaction_records` raises on the first invalid row and cannot accumulate (`src/personal_finance_agent/ledger/models.py:272-273`), so reporting every failing row requires validating before calling into the ledger.

### Assumptions

- Broker and exchange exports carry no universal per-row transaction identifier. SEPA reference fields exist in bank exports but are populated only for direct debits. Content-derived identity with the ordinal tiebreaker is therefore the only available mechanism, which is what U2 already built.
- A person imports one account per profile. A multi-account export file is not supported in U3.
- The declared period is inclusive at both boundaries and compared on the canonical `YYYY-MM-DD` string.

### Sequencing

U3a has no dependencies. U3b depends on U3a. U3c depends on U3b.

## Implementation Units

### U3a. Profile schema and the format layer

**Goal:** Turn a declared profile plus a raw file into validated rows or diagnostics, with no ledger involvement.

**Requirements:** R1, R2, R3, R8, R11, R14. Implements KTD1, KTD2, KTD3, KTD6, KTD8, KTD9, KTD10, KTD12.

**Dependencies:** none.

**Files:** `src/personal_finance_agent/connectors/__init__.py`, `src/personal_finance_agent/connectors/csv_profile.py`, `src/personal_finance_agent/connectors/csv_import.py`, `tests/test_csv_imports.py`, `tests/fixtures/imports/`.

**Approach:** Define the profile schema and validate it fully before opening the data file, so a wrong profile fails without reading a row. Locate the header by matching declared names after stripping a BOM. Parse rows through the standard-library `csv` module — never by splitting lines, because purpose fields contain embedded delimiters and newlines. Compose `purpose` by concatenating the declared columns in the declared order (R11). Normalise amounts per KTD8 and the account identifier per KTD9. Accumulate diagnostics following the pattern in `src/personal_finance_agent/aqbanking.py:239-276` rather than raising on the first failure.

**Patterns to follow:** `ParsedBalanceRow` and its diagnostics accumulation; `Diagnostic` from the ledger package. `Diagnostic` carries `previous_value` and `new_value` — do not populate them from cell contents (KTD6).

**Test scenarios:**

- Happy path: a semicolon-delimited ISO-8859-1 fixture with `DD.MM.YYYY` dates and decimal commas parses into rows.
- Happy path: each of the four amount encodings from R2 produces the same normalised amount.
- Happy path: a crypto fixture with eight decimals and a `BTC` denomination normalises without loss.
- Edge case: `1.234,56` normalises to `1234.56`, not `1.234.56`.
- Edge case: a UTF-8 BOM does not prevent the first declared column from being found.
- Edge case: metadata lines and a blank line before the header are skipped by name-matching.
- Edge case: an IBAN-kind identifier normalises grouped and compact forms to one account; an opaque-kind identifier preserves case.
- Edge case: a quoted purpose field containing the delimiter and a newline survives intact.
- Edge case: a row whose declared purpose columns are all empty is reported as a failing row, not written with an empty purpose.
- Error path: a profile naming a column the file lacks fails before any data row is read, naming the profile key and the column.
- Error path: no header row matches at all — the diagnostic reports candidate rows by column count and character class, never by content.
- Error path: a header cell containing an ANSI escape sequence is stripped and truncated before it appears in a diagnostic.
- Error path: an unparseable date reports line, column, field, and the formats attempted, and no cell content.
- Error path: a ragged row does not raise a `TypeError`.
- Error path: a mid-file decode failure reports a line number rather than aborting without position.
- Error path: a file containing NUL bytes is refused as non-text.

**Verification:** A fixture set covering all four amount encodings and both identifier kinds parses without touching a ledger.

### U3b. The import command

**Goal:** Wire the format layer to the ledger behind one CLI command.

**Requirements:** R4, R5, R6, R7, R9, R10, R12, R15. Implements KTD4, KTD5, KTD7, KTD11.

**Dependencies:** U3a.

**Files:** `src/personal_finance_agent/cli.py`, `tests/test_csv_imports.py`.

**Approach:** Follow `cmd_aq_balances` (`src/personal_finance_agent/cli.py:92-161`) in order: refuse unsafe inputs before doing any work, parse, and only then take `ledger.locked()`. Inside one lock, write the account record with `session.add`, then read the slice's stored records, keep those outside the declared period, and pass them with the parsed rows to `replace_transaction_slice` (KTD4). Note that `replace_transaction_slice` rejects non-transaction records, so the account record cannot ride along in the same call.

Name the command's flags explicitly: the input path, the profile path, the ledger path, an optional period override, and the preview flag. The input and profile paths are read from anywhere without `--allow-outside-runtime`; the ledger path keeps it (KTD11).

Exit code follows the merged pattern at `cli.py:161`: parse and validation diagnostics mean exit 2; supersession and duplicate reports are informational and exit 0. Do not pass an input path through `ensure_private_file` — that would chmod the user's own file. Do apply it to the profile, which is Mentat's own config (R12).

**Test scenarios:**

- Happy path: AE1 through the CLI, with a temporary root, asserting the account record is written.
- Happy path: AE2 — the second run leaves the ledger byte-identical.
- Happy path: AE8 — preview mode writes nothing and exits 0.
- Integration: AE3 with declared periods, and AE4 asserting the summary reports one addition and one retirement.
- Integration: AE7 — imported transactions and FinTS balances coexist in `ledger inspect --json`.
- Error path: AE5 — one bad row writes nothing and exits 2.
- Error path: an export with zero data rows and no declared period is refused.
- Error path: a symlink to the ledger, and a relative `../data/ledger.jsonl`, are both refused on resolved paths.
- Error path: a CSV that Mentat exported is refused by its `ledger_export_id` column.
- Error path: a FIFO or directory given as input is refused as not a regular file.
- Error path: the source file's permissions and content are unchanged after a failed and a successful import.
- Security: importing an outside-runtime CSV does not permit an outside-runtime ledger write.
- Regression: a renamed profile file still imports into the same slice, because `source_ref` is declared inside it (KTD10).

**Verification:** The two reproductions in KTD4 and KTD5 pass as regression tests, and the 76 merged tests are unchanged.

### U3c. Usage documentation

**Goal:** Let a person write a profile for their own export without reading the source.

**Requirements:** R13.

**Dependencies:** U3b.

**Files:** `docs/usage/csv-imports.md`, `README.md`.

**Approach:** Create `docs/usage/`, which the origin plan has promised since U1 and which does not exist. Document the profile schema field by field, give one complete synthetic example, and explain the four amount encodings. State where profiles live, that they hold an account identifier and are therefore private, and why they stay out of Git. Explain what a re-import does — that it corrects the declared period — because that is the behaviour a person will be most surprised by, and state that changing the declared `source_ref` orphans existing history. Recommend the preview mode as the first step when writing a profile. Link from `README.md`.

**Verification:** A reader can produce a working profile for a synthetic export using only this document.

## Verification Contract

- `PYTHONPATH=src python3 -m unittest discover -s tests` passes. The suite is at 76 tests on `main` (`f1e1161`); every unit adds to it and none of the 76 may be weakened.
- No file under `src/personal_finance_agent/ledger/` is modified.
- Fixtures are synthetic. No real IBAN, balance, holder name, or account identifier appears in any test, fixture, issue, or public document — while remaining recognisably German or Austrian in *format*.
- No runtime dependency is added; `pyproject.toml` stays unchanged.
- The KTD4 reproduction (overlapping ranges) and the KTD5 reproduction (a skipped row retiring history) each become a regression test.
- Diagnostics are reviewed for value echoing, including `source_ref`, before the work is declared done.
- The suite passes on the CI Python (3.12) and on 3.9.6, since the local loop runs there.

## Definition of Done

- Every requirement R1–R15 is either implemented or explicitly recorded as deferred in this plan.
- All eight acceptance examples have a named test.
- `docs/usage/csv-imports.md` exists and `README.md` links it.
- Dead-end and experimental code from approaches that did not work is removed, not left in the diff.
- Issue #3's three acceptance criteria are satisfied: a sample CSV maps into ledger transactions, unknown columns produce actionable guidance, and local mapping files stay out of Git.

## Risks & Dependencies

- **Export formats change without announcement.** comdirect is a documented case. Mitigation: fail loudly on an unrecognised structure rather than best-effort parsing, per KTD3.
- **Permanent identity, and the evidence to falsify it is unavailable.** Amount normalisation, purpose composition, and account identity all enter transaction identity, and there is no `canonical_transaction_record` equivalent to the balance migration hook. The Verification Contract forbids real provider exports in the test set, so the normal forms are validated against synthetic fixtures only. Mitigation: KTD8 and KTD9 widen the forms rather than narrowing them, and R15's preview mode lets a person inspect the mapping before any row is written. Residual risk accepted: if a normal form proves wrong after records exist, the repair is a re-import under a new `source_ref`, which orphans the old slice rather than migrating it.
- **Ordinal stability across differently-ranged exports** holds only while every occurrence of a repeated same-day payment appears in every export covering that day. A provider that paginates a single day across two files would mint parallel identities. Not tested; noted.
- **Encoding evidence is weak.** No provider publishes a format specification, and sources contradict each other on Sparkasse and DKB. Mitigation: the profile declares encoding; the importer never infers it.

## Sources & Research

- `src/personal_finance_agent/ledger/models.py:247-301` — `transaction_records`, the `complete_source_slice` contract, and ordinal assignment.
- `src/personal_finance_agent/ledger/models.py:272-273` — the per-row raise that forces the importer to validate before calling in.
- `src/personal_finance_agent/ledger/models.py:506-511` — account identity derivation and the FinTS `source_account_id` shape.
- `src/personal_finance_agent/ledger/store.py:137-191` — slice keying, cross-slice guard, and retirement.
- `src/personal_finance_agent/ledger/store.py:215-233` — the merge diagnostics that echo `source_ref`.
- `src/personal_finance_agent/cli.py:92-161` — the connector-to-ledger wiring pattern, including the exit-code rule.
- `src/personal_finance_agent/aqbanking.py:239-276` — diagnostics accumulation to follow.
- `docs/plans/2026-07-02-002-fix-local-banking-security-boundaries-plan.md` — the security posture this plan inherits; its KTD6 is the no-echo rule.
- `docs/explainers/2026-09-01-u2-local-ledger-core.html` — why identity is content-derived, and the false-merge versus false-duplicate reasoning KTD5 rests on.
- `docs/solutions/workflow-issues/delegating-implementation-to-headless-cli-coding-agents.md` — how this work is briefed and verified if delegated.
- hledger CSV rules (`skip`, `separator`, `encoding`, `decimal-mark`, `date-format`) and GnuCash's import matcher — both declare formats explicitly rather than inferring them, and GnuCash treats duplicate detection as reviewable rather than guaranteed.
- Firefly III importer roles — the four-way amount-encoding taxonomy behind R2 and KTD2.
- beangulp issue #95 — deduplication instability at overlapping export-window boundaries, the failure class KTD4 and KTD7 address.
