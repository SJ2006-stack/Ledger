# Ledger

Ledger is an accounts-payable agent. You talk to it in one chat, attach an invoice, and it records the decision on a SQL ledger grid. Python makes the accounting decision and runs each ledger action. Groq (`qwen/qwen3.8-27b`) reads the PDF and explains what happened.

## What it does

- Reads the vendor, invoice number, date, purchase order, goods receipt, and amounts.
- Checks the vendor, purchase order, goods receipt, and tax arithmetic.
- Writes a clean match as `NEEDS_APPROVAL`. A mismatch is `NEEDS_REVIEW` with a code such as `QUANTITY_MISMATCH`.
- Fills the grid one cell at a time, then reads the row back from SQL. The task counts only when that read matches.
- Holds a duplicate, an unknown vendor, a bad scan, or an invoice with missing fields. Those do not get a new row.
- After a verified result, emails it with AgentMail when you give an address, and posts it to Slack. A named `#channel` wins. Otherwise the post goes to `#general`.
- Answers a message with no file: what it can do, what it can access, and the current progress.

It asks only for a detail it cannot invent, such as an email address or which Helios vendor to use. It does not type `APPROVED` or `POSTED`. A person approves a `NEEDS_APPROVAL` row. It does not send payments.

`CONTEXT.md` is the longer note on those rules.

## Layout

```
backend/          FastAPI app, ledger rules, and the computer-use worker
frontend/         React chat and ledger grid
mini_hyperbots_invoice_pack/   synthetic invoices for the demo
requirements.txt  Python dependencies
.env.example      keys the server reads
```

SQLite is created at `backend/data/accounting.db` on first run. That file stays on the machine.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set GROQ_API_KEY
cd frontend && npm install && npm run build && cd ..
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Attach `northwind-inv-2001.pdf` and send "Look after this invoice." If that invoice is already on the ledger, the agent holds it instead of writing a second row.

`EXTRACTOR=template` uses a fixed reader so tests do not call a model. `CUA_HEADLESS=0` is the local default.

## Keys

Copy `.env.example` to `.env`. The server needs `GROQ_API_KEY` to read invoices and to talk.

- AgentMail: `AGENTMAIL_API_KEY` and `AGENTMAIL_INBOX_ID`
- Slack: `SLACK_BOT_TOKEN` or `SLACK_ACCESS_TOKEN`, and optionally `SLACK_DEFAULT_CHANNEL`

A missing key is reported in the chat. The service stays disconnected.

## Tests

```bash
PYTHONPATH=backend .venv/bin/pytest -q -m "not live"
PYTHONPATH=backend .venv/bin/pytest -q -m live
```

Ground-truth decisions live in `backend/tests/ground_truth.py`. The agent package does not import them.

## Limits

- Synthetic invoices only.
- One SQLite file. No sign-in and no multi-company books.
- A poor scan needs a local Tesseract install and stops for a clearer copy.
- The worker has no shell and no general filesystem tool.
