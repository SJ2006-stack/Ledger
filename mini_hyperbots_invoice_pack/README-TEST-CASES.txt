Mini-Hyperbots synthetic invoice pack
======================================

All documents in this pack are fictional, synthetic test data. They are not valid tax invoices and must not be used for payment, accounting, tax filing, or proof of a real transaction.

Files:
- northwind-inv-2001.pdf: clean match; PO-1001 and GRN-1001 both show 100 units at INR 50.00; expected total INR 5,900.00.
- harbor-inv-2002.pdf: quantity mismatch; PO ordered 100, GRN received 90, invoice bills 100.
- apex-inv-3003.pdf: duplicate; INV-3003 already exists in the simulated books.
- northwind-inv-2200.pdf: PO-9999 does not exist; GRN blank.
- northwind-inv-2100.pdf: tax mismatch; subtotal INR 5,000.00 at 18% should yield INR 900.00 tax, but invoice says INR 100.00 and total INR 5,100.00.
- helios-inv-5005.pdf: ambiguous vendor; both Helios Metals (PO-5005, Plate HM @ 1,000) and Helios Metal Works (PO-5006, Plate HW @ 1,100) are possible.
- cascade-inv-7001.pdf: unknown vendor; PO and GRN blank.
- scan-unreadable.pdf: near-blank, low-contrast text; expected outcome is safe extraction failure and a request for a readable copy.
- northwind-inv-2010.pdf: inject a simulated save failure; retry safely and verify no duplicate.
- northwind-inv-2011.pdf: inject a lost-response condition after a successful commit; check persisted state before retrying.
- northwind-inv-2012.pdf: price mismatch; invoice unit price INR 999.00 vs PO unit price INR 50.00.

Suggested simulated book fixtures:
- Vendor Northwind Supplies; Tax ID 27AABCU9603R1ZM.
- PO-1001: SKU-WA / Widget A, ordered quantity 100, unit price INR 50.00.
- GRN-1001: SKU-WA / Widget A, received quantity 100.
- PO-2002: SKU-BB / Bolt B, ordered quantity 100, unit price INR 10.00.
- GRN-2002: SKU-BB / Bolt B, received quantity 90.
- PO-3003 and GRN-3003: SKU-CX / Cable X, quantity 10, unit price INR 200.00.
- PO-5005: Plate HM, quantity 5, unit price INR 1,000.00.
- PO-5006: Plate HW, quantity 5, unit price INR 1,100.00.
- Pre-existing invoice: Apex Industrial / INV-3003.
- Pre-existing invoice: Northwind Supplies / INV-2001 may be used for clean-match seed testing only if the workflow expects that invoice to be already present; otherwise keep it absent for first-run success.

Note on tax display:
The invoice PDFs use the requested single Tax Rate / Tax / Total layout. The GST tax amounts are deliberately specified by the test cases. The synthetic documents do not attempt to represent all legally required Indian GST invoice particulars.
