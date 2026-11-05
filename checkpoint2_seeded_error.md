# Checkpoint 2 — Seeded-Error Verification

Seeded diluted EPS: $21.76
Verifier mark: wrong
Correction: $20.76
XBRL tag: us-gaap:EarningsPerShareDiluted
Period: 2025-09-01 to 2026-08-30
10-K accession: 0000909832-26-000093

The test changed the report value from the filing-supported $20.76 to $21.76 while leaving the saved filing texts and facts unchanged. Running `--verify-only` detected the seeded value as wrong and corrected it to $20.76.
