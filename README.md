# Mentat

Mentat is a local-first toolkit for building your own personal finance single source of interaction.

The goal is simple: connect to your own banks and finance exports locally, normalize the data, and make it available to scripts, spreadsheets, dashboards, and agents without giving a cloud service your banking credentials.

The first focus is Europe, especially Germany and Austria, where FinTS/HBCI and bank exports are common building blocks.

The Python package and CLI module currently retain the `personal_finance_agent` name during the pre-alpha phase.

## Status

Pre-alpha. The first working backend is an AqBanking/FinTS wrapper for local account and balance exports.

## Principles

- Local-first by default.
- No bank credentials in Git.
- No hosted service required.
- Explicit connectors, explicit exports.
- Built for personal use first, automation second.
- European banking realities over glossy abstractions.

## First Use Case

Fetch balances from a German FinTS bank through AqBanking and write a normalized CSV:

```bash
python3 -m personal_finance_agent.cli aq versions --root .
python3 -m personal_finance_agent.cli aq add-pintan-user \
  --root . \
  --bank-code BANKLEITZAHL \
  --user-id ONLINE_BANKING_LOGIN \
  --server-url FINTS_SERVER_URL
python3 -m personal_finance_agent.cli aq get-accounts --root . --user USER_ID --safe-pin-user USER_ID
python3 -m personal_finance_agent.cli aq balances --root . --safe-pin-user USER_ID
```

`--safe-pin-user` uses a temporary AqBanking PIN file and validates the PIN before AqBanking sees it. Some AqBanking PIN-file keys do not support whitespace in the FinTS login value; in that case the wrapper fails before asking for a PIN instead of creating a broken PIN file. Run without `--safe-pin-user` if you intentionally want AqBanking to prompt directly.

By default, balance context and CSV files are written under the runtime `data/` directory. Use `--allow-outside-runtime` only when you intentionally want a context or CSV path outside that local boundary. Non-standard AqBanking installs can be selected with `aq --aqbanking-cli PATH` and `aq --aqhbci-tool4 PATH`.

For local development from this checkout:

```bash
PYTHONPATH=src python3 -m personal_finance_agent.cli version
PYTHONPATH=src python3 -m personal_finance_agent.cli validate-pin
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Backends

Planned connector families:

- AqBanking / FinTS for Germany and parts of the DACH ecosystem.
- CSV/PDF/manual imports for brokers and banks without useful APIs.
- Coinbase and other exchange APIs where read-only keys are available.
- Optional Open Banking providers where they make sense for individuals.

## Security

Generated data and runtime configuration are local-only:

- `data/`
- `config/aqbanking.local/`
- `config/aqbanking.home.local/`
- `.env*`
- `config/*.local.json`

PINs are never stored by the tool. The AqBanking wrapper can create a temporary PIN file with restrictive permissions and delete it immediately after use.

Runtime directories are created with private POSIX permissions where supported. CSV exports are spreadsheet-safe by default: cells that could be interpreted as spreadsheet formulas are neutralized during CSV writing.

## Roadmap

- Account discovery and balance export through AqBanking.
- Transaction export and normalization.
- Import adapters for brokers and crypto exchanges.
- A stable local data model.
- Agent-friendly command outputs.
- Optional dashboard/reporting layer.
