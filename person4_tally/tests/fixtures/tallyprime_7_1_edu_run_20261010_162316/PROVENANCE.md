# Real TallyPrime capture (do not edit these files)

- Captured 2026-10-10 by Harshith with `tally_identity_probe/Run-FinLedger-Capture.ps1` on Windows PowerShell 5.1.26100,
  TallyPrime EDU 7.1, company "FinLedger Test Company" (the only loaded company), port 9000.
- Source: the probe's `captures\run_20261010_162316\` folder; bytes copied unchanged.
- One Purchase voucher (No. 1, 1-Apr-2026, FinLedger Test Vendor, ref TEST-INV-001, narration
  `Test purchase finledger:document_id=9d4f3d5d-499e-41db-b510-e98348e5b503`). Between steps 04 and 05 its amount was
  edited by hand from 1180.00 to 2000.00. (summary.txt's "new amount typed by user: 1180" is a typing slip; the XML
  shows 2000.00.)
- summary.txt is the launcher's quick look, not evidence.

SHA256:
```
ea98a1baa3ec311fa78cf2f643eef25f1ec87d939ba2be589676586edbd27f07  response_01_companies.xml
8c2cac008b5ff190eb1d435f8373bdcf6e34bac5091c9a58f7865f135a90eb79  response_02_day_before_edit.xml (= 03, = 04)
e89ddd0bbac898f89f33254d0949b2aaf35adc9b8894b9a1c04ca22d26b4eb77  response_05_day_after_edit.xml (= 06)
```
