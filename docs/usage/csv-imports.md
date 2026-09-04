# Importing CSV transactions

Mentat imports delimited transaction exports through a local JSON profile. The
profile names every column and declares the file's encoding and number/date
formats; the importer does not guess them. One profile represents one account.

Start with `--preview`. It performs the complete parse and normalization and
prints the mapped rows and declared period as JSON, but does not open or create
the ledger:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli ledger import-csv \
  --root . \
  --input ~/Downloads/dach-demo-export.csv \
  --profile config/dach-demo-broker.local.json \
  --preview
```

When the preview is correct, omit `--preview` to write to the default ledger at
`data/ledger.jsonl`:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli ledger import-csv \
  --root . \
  --input ~/Downloads/dach-demo-export.csv \
  --profile config/dach-demo-broker.local.json
```

## A complete profile

This synthetic profile matches a semicolon-delimited DACH-style export whose
header includes `Buchungstag;Text;Notiz;Betrag;Waehrung`:

```json
{
  "source": "DACH Demo Broker",
  "source_ref": "dach-demo-broker-primary",
  "account": {
    "identifier": "DEMO-DEPOT-AT-0042",
    "identifier_kind": "opaque",
    "name": "Demo-Depot",
    "currency": "EUR"
  },
  "period": {
    "start": "2026-01-01",
    "end": "2026-01-31"
  },
  "format": {
    "encoding": "iso-8859-1",
    "delimiter": ";",
    "quotechar": "\"",
    "decimal_mark": ",",
    "thousands_separator": ".",
    "date_format": "%d.%m.%Y"
  },
  "columns": {
    "booking_date": "Buchungstag",
    "purpose": ["Text", "Notiz"],
    "denomination": "Waehrung"
  },
  "amount": {
    "strategy": "signed",
    "column": "Betrag"
  }
}
```

The header is located by these declared names, so metadata and blank lines may
appear before it and column order may change. A UTF-8 BOM on the first header
cell is accepted. A row is confirmed as the header only when it contains every
declared name; near matches are treated as preamble candidates and scanning
continues. Header names otherwise must match exactly, and undeclared extra
columns on a confirmed header are accepted.

## Profile fields

- `source`: a stable label for the provider. It participates in account and
  transaction identity, so do not casually change it after importing.
- `source_ref`: a stable, non-sensitive label for this account's import slice.
  It must begin with a letter, be at most 64 characters, and contain only
  letters, digits, `.`, `:`, `_`, or `-`. Never put an IBAN or account number
  here. **Changing `source_ref` orphans the existing slice's history:** the new
  value does not rename or migrate records already stored under the old value.
- `account.identifier`: the provider's stable identifier for the one account in
  this export.
- `account.identifier_kind`: `iban` or `opaque`. IBAN values have all whitespace
  removed and are upper-cased. Opaque identifiers have surrounding whitespace
  removed but remain case-sensitive.
- `account.name`: an optional display name. It may be omitted or set to `null`.
- `account.currency`: an optional account-level currency/denomination. It may be
  omitted or set to `null`; each transaction still reads its denomination from
  the CSV.
- `period.start` and `period.end`: the inclusive dates covered by the export,
  both in `YYYY-MM-DD` form. The period is about what the provider included, not
  merely the earliest and latest rows present.
- `format.encoding`: a Python codec name such as `utf-8` or `iso-8859-1`.
- `format.delimiter`: exactly one character, commonly `;` for DACH exports.
- `format.quotechar`: exactly one character, commonly `"` (written as `\"` in
  JSON).
- `format.decimal_mark`: exactly one character, such as `,`.
- `format.thousands_separator`: either one character, such as `.`, or the empty
  string when the export has no thousands separator.
- `format.date_format`: a `strptime` format, such as `%d.%m.%Y` for
  `31.01.2026` or `%Y-%m-%d` for `2026-01-31`.
- `columns.booking_date`: the exact header name of the booking-date column.
- `columns.purpose`: one or more exact header names, in order. Non-empty values
  are joined mechanically with ` | `; Mentat does not infer merchants,
  categories, or tags.
- `columns.denomination`: the exact header name containing an alphanumeric code
  such as `EUR` or `BTC`. Mentat normalizes it to uppercase.
- `amount`: selects one of the four strategies below and declares its columns.

## Amount strategies

All strategies use the declared decimal mark and thousands separator. For
example, `1.234,560` becomes `1234.560`, preserving the source's significant
decimal places. The transaction amount combines that number with the normalized
denomination, for example `1234.560 EUR`.

Use `signed` when the amount column already contains the desired sign:

```json
"amount": {"strategy": "signed", "column": "Betrag"}
```

Use `inverted` when the export's sign convention is the opposite of the
ledger's convention (positive input becomes negative and negative input becomes
positive):

```json
"amount": {"strategy": "inverted", "column": "Betrag"}
```

Use `debit_credit_columns` when debits and credits are in separate columns.
Exactly one of the two cells must be populated on each row; debit becomes
negative and credit becomes positive:

```json
"amount": {
  "strategy": "debit_credit_columns",
  "debit_column": "Soll",
  "credit_column": "Haben"
}
```

Use `indicator` when one amount column is paired with a separate direction
column. Declare non-empty, non-overlapping, case-sensitive values for both
directions:

```json
"amount": {
  "strategy": "indicator",
  "column": "Betrag",
  "indicator_column": "Richtung",
  "debit_values": ["S", "BELASTUNG"],
  "credit_values": ["H", "GUTSCHRIFT"]
}
```

## Profiles, privacy, and paths

Keep profiles under `config/` with a `.local.json` suffix, for example
`config/dach-demo-broker.local.json`. The repository's `.gitignore` excludes
`config/*.local.json` because a profile contains an account identifier and must
not be committed. On POSIX systems Mentat sets an existing profile to mode
`0600` before reading it. You can also make it private when creating it:

```bash
umask 077
mkdir -p config
touch config/dach-demo-broker.local.json
```

The CSV and profile are read-only inputs and may be anywhere on the filesystem;
Mentat does not move, rewrite, or change the CSV's permissions. The ledger is a
runtime write and remains bounded to `<root>/data/` by default. A relative
`--ledger` value is resolved under that directory. Only when you intentionally
write a ledger elsewhere should you pass both an absolute ledger path and
`--allow-outside-runtime`:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli ledger import-csv \
  --root . \
  --input ~/Downloads/dach-demo-export.csv \
  --profile config/dach-demo-broker.local.json \
  --ledger /absolute/private/location/ledger.jsonl \
  --allow-outside-runtime
```

That flag applies to the ledger path; it is not needed for the input or profile.
Mentat refuses non-regular inputs, its own ledger, lock, and lineage paths, its
balance export path, and CSV files produced by Mentat itself. Mentat-exported
files are recognized by the `ledger_export_id` header column even when metadata
or blank lines appear before that header.

## Periods and re-imports

The declared period controls correction, and both endpoints are inclusive.
During a re-import, transactions in that period are replaced by the newly
parsed rows while transactions outside it remain. This permits overlapping
exports: importing January--June and later March--September preserves January
and February, while March--June is corrected from the later export.

Do not derive the period from the rows. A provider may withdraw a boundary row,
backfill an older booking, or export an empty period. To override the profile for
one invocation, use:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli ledger import-csv \
  --root . \
  --input ~/Downloads/dach-demo-export.csv \
  --profile config/dach-demo-broker.local.json \
  --period 2026-01-01 2026-03-31 \
  --preview
```

`--period` replaces the profile period for that invocation. You may omit
`period` from the profile only when every invocation supplies this override. An
empty export is accepted only with an explicit declared or overridden period;
on a real import it retires the stored transactions inside that period.

Parsing is all-or-nothing. If any data row is invalid, Mentat reports the
diagnostics it found, exits with status 2, and writes neither the account nor any
transaction. There is deliberately no skip-invalid-rows option: silently
omitting a row during a corrective re-import could delete or duplicate history.

After importing, inspect all transaction records with:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli ledger inspect \
  --root . \
  --record-type transaction \
  --json
```
