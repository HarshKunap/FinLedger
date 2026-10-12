# Real TallyPrime ledger-masters capture, 2026-10-11 01:34 (IST)

Captured by `tally_identity_probe/Run-Masters-Capture.ps1` on the project owner's PC against TallyPrime EDU 7.1 with
`FinLedger Test Company` (GUID `7a5f42d4-ec1d-4aab-9992-e2e217bcb773`) loaded. Read-only: three Export requests.

The six XML files are byte-for-byte what was sent and received (sha256 in `masters_summary.txt`). The only edit in this
folder is in `masters_summary.txt`: the local Windows folder path was replaced with a placeholder. The data is a
throwaway test company (default ledgers plus `FinLedger Test Vendor` / `FinLedger Test Purchase`); no real business
or personal data.

What the bytes show (checked by `tests/test_real_masters.py`):
- UTF-8, no BOM, CRLF line endings; one `ENVELOPE/BODY/DATA/COLLECTION` with `ISMSTDEPTYPE="Yes" MSTDEPTYPE="8"`.
- A `DESC/CMPINFO` block precedes `DATA`; its counters were all 0 here but non-zero in the 2026-10-10 company
  capture, so they are never used.
- Four `LEDGER` rows. The name is the `NAME` attribute; there is **no direct `NAME` child**. The name appears again
  as `LANGUAGENAME.LIST/NAME.LIST/NAME` (where Tally also lists aliases).
- `GUID` = `<company GUID>-<8 hex>` for every ledger (first real evidence for masters, not only vouchers).
- `PARENT TYPE="String"`; the reserved group is written `&#4; Primary` (illegal in XML 1.0, normalised by the parser).
- `RESERVEDNAME` is non-empty for the built-in `Profit &amp; Loss A/c`; `&amp;` is escaped normally.
- The company list before and after is identical (same GUID): the identity check passed on real Tally.
