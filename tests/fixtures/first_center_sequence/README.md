These are frozen, unmodified 5m OHLC and native snapshot files from completed
screening batch `2bdf80835163459aab955372c1cd9be0` (2026-10-13). `cases.json`
retains the published candidate and its checksum manifest. Tests validate the
snapshot against its input data before deriving the sequence.

- SZ.301119: user-reported third-buy preview; scalar 1, but two earlier centers.
- SZ.001217: confirmed third buy; scalar reset to 1 after overlapping envelopes.
- SZ.002022: confirmed third buy; scalar already 2, but reader bypassed the filter.
- SH.688349: a verified first-center third buy retained as a positive control.

This admission policy belongs to the application's native segment framework;
it does not label that framework a fully recursive Chan movement proof. Point
identities and the original snapshots remain unchanged by the selection filter.
