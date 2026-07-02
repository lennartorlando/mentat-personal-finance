# Open Source Direction

`finlocal` should become a local-first CLI for people who want bank data exports without handing credentials to a cloud service.

## Publishable Core

- AqBanking command orchestration
- PIN shape validation before FinTS encoding
- Temporary PIN file handling
- Normalized balance CSV exports
- Local runtime directories ignored by Git
- Documentation for safe operation

## Personal Overlay

Keep these out of the open-source package:

- Personal bank presets
- Local AqBanking config
- Generated balances and transactions
- Google Drive sync paths
- `.git/`, `.env*`, `config/*.local.json`, `data/`

## First Product Scope

1. Configure a PIN/TAN FinTS user.
2. Fetch SEPA accounts through AqBanking.
3. Fetch balances into a normalized CSV.
4. Later: transactions, broker exports, Coinbase API, and monthly scheduling.

## Security Rules

- Never log or echo PINs.
- Validate PIN length and ASCII compatibility before AqBanking sees it.
- Use temporary PIN files only, with `0600` permissions, and delete them immediately.
- Treat local account config as sensitive metadata.
- Keep cloud sync explicit and allow-listed.

