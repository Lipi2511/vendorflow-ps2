# VendorFlow

A working procurement case study for GST-registered Indian companies. Submit an editable fictional application and two PDFs, inspect the evidence behind the onboarding decision, and resolve a document issue under the same reference.

## Run

Use Python 3.12. In a virtual environment, run:

```sh
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

For the regression suite, install pytest and run `python -m pytest -q`. For Streamlit Community Cloud, select the repository, branch `main`, entry point `app.py`, and Python 3.12. No application secrets or external verification services are required.

## Walkthrough

1. Open **New application**. Select **New supplier**, inspect or edit the data, and run the checks. Selecting an example attaches genuine, generated PDFs. Uploaded files take precedence. Custom submissions start empty. **Download sample kit** contains the field values and supporting PDFs.
2. Open **Changed bank account** as a new application. The Juniper documents agree with the form. The register holds account ending **1824** and the application requests **8479**. Procurement owns independent verification through the established contact. The original record is preserved.
3. Run **Wrong document**. Read the vendor's request for bank proof, select **Correct and resubmit**, then attach the corrected sample bank letter or upload the corrected PDF from the kit. Review attempts 1 and 2 under the same reference.
4. **Different account holder** compares Fern Studio to Fern Retail. **Blocked supplier** uses an exact documented identity match to a fictional purchasing block.
5. Return to the dashboard to filter cases and inspect any attempt. A repeat clean application with the same GSTIN finds the newly enrolled supplier. To rehearse the clean path again, open the base URL without its workspace query string.

## Policy and data

| Evidence | Action |
| --- | --- |
| Valid, consistent new application and no matching register record | Approve internal onboarding and add the supplier within this workspace |
| Incomplete, unreadable, ambiguous or inconsistent evidence | Pending, with a specific request to the vendor |
| Existing active supplier, including changed account number or IFSC | Pending with procurement for reconciliation or independent verification |
| Blocked internal record with matching GSTIN and legal identity in the certificate and form | Reject with a vendor-safe review route |
| Possible blocked identity without corroboration | Pending with procurement |
| Unexpected system error | Failed run with retry on the same reference |

Text-based PDFs up to 5 MB and ten pages are supported. Use labelled fields as illustrated by the sample kit. Bank evidence includes account holder, account number and IFSC. GST evidence includes legal name and GSTIN. OCR, external GST status, independent bank verification and actual payment release are outside the demo. Name comparison ignores case, punctuation and spacing. Trade names do not substitute for legal identity. Vendor messages appear in the app. A procurement hold requires a human operating through the buyer’s independent process.

Each workspace starts with the same two fictional reference suppliers. Input examples never select decisions. Approvals, checks, stage timestamps, follow-up and attempts are stored in SQLite. Account and GSTIN comparisons use fingerprints in the register and masked values in history. Source PDFs, full account numbers and contact email are held in session memory only. After a fresh session, reattach evidence to correct a custom submission. Use synthetic data. The workspace URL grants access to its demo records.

The default database lives in `.runtime/vendorflow.sqlite3`. Set `VENDORFLOW_DB` to use another path. History survives app refreshes and process restarts on the same disk. A host that replaces local storage can reset it. For durable production retention use a managed database or persistent volume.

The supplier-register check and enrollment share a transaction. A repeated submission event is idempotent. Corrections check the previous attempt and cannot overwrite a newer result or release a procurement hold. Interrupted runs become retryable after ten minutes.

## Design references

The existing-supplier check and linked correction flow follow [Oracle's supplier registration process](https://docs.oracle.com/en/cloud/saas/procurement/26d/oaprc/supplier-registration-process.html) and [registration approval actions](https://docs.oracle.com/en/cloud/saas/procurement/26b/oaprc/supplier-registration-approval-actions.html). Independent handling of changed banking details follows the [Western Australian Auditor General’s supplier master guidance](https://audit.wa.gov.au/reports-and-publications/reports/supplier-master-files-better-practice-guide/). The legal name and trade name distinction is documented in the [GST taxpayer guide](https://tutorial.gst.gov.in/userguide/taxpayersdashboard/Search_Taxpayer_manual.htm).

## Optional AI

Set `OPENAI_API_KEY` in Streamlit Community Cloud secrets or your local environment to enable extraction fallback and vendor email drafting. `VENDORFLOW_MODEL` overrides the default `gpt-4o-mini`. Use fictional documents only. On API failures, unresolved extraction routes to procurement while email drafting uses deterministic wording.
