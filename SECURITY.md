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

Use `--safe-pin-user` for interactive AqBanking operations whenever possible. If the configured FinTS login would produce an unsupported AqBanking PIN-file key, the wrapper fails before prompting for a PIN.

The wrapper resolves AqBanking executables before any PIN prompt, keeps generated context and CSV files under the runtime `data/` directory by default, and requires an explicit override for external paths.

Runtime directories and generated files are private on POSIX systems where supported. CSV exports neutralize spreadsheet formula prefixes before writing.

## Reporting

Please report security issues privately first. Until a dedicated email exists, open a minimal GitHub issue that says a private security report is needed, without including secrets or exploit details.
