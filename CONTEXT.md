# Ledger agent

This is the accounts-payable intern in `intern/`. You talk to it in one chat. It reads an invoice, checks it against the purchase order and the goods receipt, and records the result on the SQL ledger grid. Python makes the accounting decision and runs the computer-use actions. Groq explains what happened in plain language.

## What is already built

The page is a conversation. There is no Run button. You can attach one or more invoices. A message with no file is a conversation: Groq answers from a fixed system prompt and says what the agent can do, what it can access, and the current progress.

Behind that chat:

- Groq (`qwen/qwen3.8-27b`) reads the PDF fields. `EXTRACTOR=template` uses a fixed reader so tests do not call a model.
- Python matches the vendor, purchase order, and goods receipt, checks the tax arithmetic, and decides whether a row may be written.
- The worker keeps a plan and runs one action at a time: open the ledger, check each tab, add one row, type each cell, save, and read it back from SQL.
- A task is verified only when a fresh SQL read matches. A missing confirmation is recorded, and the task is not left running.
- After each confirmed action the plan and the evidence are saved. A restart reads the ledger before it writes. A matching row is verified. A partial or conflicting row is blocked for review.
- Python rejects a status or amount the rules did not produce. A second blank row is rejected.
- After a verified result, the same turn can type and send that result with AgentMail, and type and post it in Slack. The message names the invoice, the status, and any exception. A failed or unverified task does not get a success claim, and the composer stays closed.

SQLite stores the ledger rows, the lookup tables, tasks, and the older invoice list. Those older invoices are labeled Legacy. A ledger run does not insert a second legacy invoice. Human approval sets a `NEEDS_APPROVAL` row to `APPROVED` and reads it back. `APPROVED` means a person approved that row. It does not mean the invoice was paid or posted.

## What the agent does on its own

It does not ask permission. If the message and the attachment are enough, it acts.

- You attach an invoice and say to look after it. The grid shows the vendor, purchase order, goods receipt, and the new ledger row filling in.
- A clean match is written as `NEEDS_APPROVAL`. A mismatch is written as `NEEDS_REVIEW` with a code such as `QUANTITY_MISMATCH`.
- A duplicate, an unknown vendor, a poor scan, or missing fields does not get a new row.
- If you include an email address, the mail composer types the verified result and AgentMail sends it. If you ask for Slack and do not name a channel, the message is typed into `#general` and posted. A named `#channel` wins.
- If you ask for mail and give no address, it asks who to email and does not send.

## When it stops and asks you

- A message with no file is a conversation about what the agent can do and where the work stands.
- You asked for email and did not give an address: "Who should I email?"
- Two vendors match, as with Helios: it pauses and you pick one before anything is written.

## What it will not decide

- It will not post an invoice the rules rejected.
- It will not invent fields from a bad scan.
- It will not type `APPROVED` or `POSTED`. A person approves a `NEEDS_APPROVAL` row on the ledger.
- It has no shell and no general filesystem tool.

## How to try it

Start the server from `intern/` and open http://127.0.0.1:8000. Attach `northwind-inv-2001.pdf` and send "Look after this invoice." The grid shows the row as it is written. If that invoice is already on the ledger, the agent holds it instead of writing a second row.

Keys for the outbound tools are listed in `.env.example`: `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX_ID`, `SLACK_BOT_TOKEN`, and `SLACK_DEFAULT_CHANNEL`.
