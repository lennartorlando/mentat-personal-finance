# Security Policy

Personal Finance Agent touches sensitive financial workflows. Treat every local runtime file as private.

## Do Not Commit

- Bank credentials
- PINs, TANs, recovery codes, API secrets
- AqBanking local config
- Generated balances or transactions
- Personal bank identifiers unless they are intentionally shared examples

## Runtime Safety

The tool validates FinTS PIN shape before handing it to AqBanking because some AqBanking error paths can print too much diagnostic detail.

Use `--safe-pin-user` for interactive AqBanking operations whenever possible.

## Reporting

Please report security issues privately first. Until a dedicated email exists, open a minimal GitHub issue that says a private security report is needed, without including secrets or exploit details.

