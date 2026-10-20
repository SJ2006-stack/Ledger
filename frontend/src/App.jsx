import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson, sampleFile, streamChat } from "./api.js";

const INSTRUCTION =
  "Read the uploaded invoice, match it to the purchase order and goods receipt, and post it for approval only when the vendor, quantities, prices, and totals agree.";

export default function App() {
  const [view, setView] = useState("chat");
  const [queueOnly, setQueueOnly] = useState(false);
  const [samples, setSamples] = useState([]);
  const [invoices, setInvoices] = useState([]);
  const [sheetRows, setSheetRows] = useState([]);
  const [ledger, setLedger] = useState({});
  const [gridOrigin, setGridOrigin] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [pdfChoice, setPdfChoice] = useState(null);
  const [orders, setOrders] = useState([]);
  const [receipts, setReceipts] = useState([]);
  const [instruction, setInstruction] = useState(INSTRUCTION);
  const [files, setFiles] = useState([]);
  const [previewUrl, setPreviewUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState("");
  const [events, setEvents] = useState([]);
  const [task, setTask] = useState(null);
  const [selected, setSelected] = useState(null);
  const [thread, setThread] = useState(null);
  const [narrating, setNarrating] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const running = useRef(false);
  const replayGen = useRef(0);
  const shownPreview = useRef("");
  const threadRef = useRef(null);

  async function refresh() {
    const [invoiceRows, orderRows, receiptRows, sampleRows, book, taskRows] = await Promise.all([
      loadJson("/api/invoices", []),
      loadJson("/api/purchase-orders", []),
      loadJson("/api/goods-receipts", []),
      loadJson("/api/samples", []),
      loadJson("/api/ledger", { sheets: {} }),
      loadJson("/api/tasks", []),
    ]);
    setInvoices(invoiceRows.value);
    setTasks(Array.isArray(taskRows.value) ? taskRows.value : []);
    const sheets = book.value.sheets || {};
    setLedger(sheets);
    setSheetRows((sheets.Invoice_Ledger?.rows || []).filter((row) => ["NEEDS_APPROVAL", "NEEDS_REVIEW", "APPROVED"].includes(row.Status)));
    setOrders(orderRows.value);
    setReceipts(receiptRows.value);
    setSamples(sampleRows.value);
    const failed = [invoiceRows, orderRows, receiptRows, sampleRows, book, taskRows].find((item) => item.error);
    if (failed) setError(failed.error);
  }

  useEffect(() => {
    refresh().catch((exc) => setError(exc.message));
  }, []);

  useEffect(() => {
    const node = threadRef.current;
    if (!node || busy) return;
    node.scrollTop = node.scrollHeight;
  }, [busy, messages, error, view]);

  function rememberPreview(next) {
    setPreviewUrl((current) => {
      if (current && current.startsWith("blob:") && current !== shownPreview.current) {
        URL.revokeObjectURL(current);
      }
      return next ? URL.createObjectURL(next) : "";
    });
  }

  async function addSample(name) {
    if (!name || files.some((item) => item.name === name)) return;
    setPicking(true);
    setError("");
    try {
      const next = await sampleFile(name);
      setFiles((current) => (current.some((item) => item.name === next.name) ? current : [...current, next]));
      if (!files.length) rememberPreview(next);
    } catch (exc) {
      setError(exc.message);
    } finally {
      setPicking(false);
    }
  }

  function removeFile(name) {
    setFiles((current) => current.filter((item) => item.name !== name));
  }

  function addLocalFiles(list, open) {
    const picked = [...list].filter((file) => file && file.size);
    if (!picked.length) return;
    setFiles((current) => {
      const names = new Set(current.map((item) => item.name));
      const next = [...current];
      for (const file of picked) {
        if (names.has(file.name)) continue;
        names.add(file.name);
        next.push(file);
      }
      return next;
    });
    const first = picked[0];
    rememberPreview(first);
    if (open) {
      setPdfChoice({
        key: `local-${first.name}`,
        number: first.name,
        vendor: "This computer",
        status: "Attached",
        source: first.name,
        url: URL.createObjectURL(first),
      });
      setView("invoices");
    }
  }

  function resetThread() {
    replayGen.current += 1;
    if (shownPreview.current.startsWith("blob:")) URL.revokeObjectURL(shownPreview.current);
    shownPreview.current = "";
    setPreviewUrl((current) => {
      if (current && current.startsWith("blob:") && current !== shownPreview.current) {
        URL.revokeObjectURL(current);
      }
      return "";
    });
    setView("chat");
    setNarrating(false);
    setThread(null);
    setEvents([]);
    setTask(null);
    setSelected(null);
    setFiles([]);
    setPdfChoice((current) => {
      if (current?.url?.startsWith("blob:")) URL.revokeObjectURL(current.url);
      return null;
    });
    setError("");
    setInstruction(INSTRUCTION);
    setMessages([]);
    setDraft("");
  }

  async function resetDemo() {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    try {
      await postJson("/api/ledger/reset");
      resetThread();
      await refresh();
    } catch (exc) {
      setError(exc.message);
    } finally {
      running.current = false;
      setBusy(false);
    }
  }

  async function onSend(event) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || running.current) return;
    const attached = files;
    const priorId = thread?.source === "live" ? task?.id || "" : "";
    setDraft("");
    setMessages((current) => [...current, { role: "user", text: message }]);
    if (!priorId) {
      replayGen.current += 1;
      shownPreview.current = previewUrl;
      setGridOrigin(ledger);
      setThread({
        source: "live",
        instruction: message,
        filename: attached.map((item) => item.name).join(", "),
        previewUrl,
      });
      setEvents([]);
      setTask(null);
      setSelected(null);
    }
    running.current = true;
    setView("chat");
    setNarrating(false);
    setBusy(true);
    setError("");
    try {
      const result = await streamChat(message, attached, priorId, (frame) => {
        if (frame.event === "audit") setEvents((current) => [...current, frame.data]);
        if (frame.event === "say" && frame.data?.text) {
          setMessages((current) => [...current, { role: "agent", text: frame.data.text }]);
        }
      });
      if (result?.task) setTask(result.task);
      await refresh();
    } catch (exc) {
      setError(exc.message);
    } finally {
      running.current = false;
      setBusy(false);
    }
  }

  async function clarify(vendorName) {
    if (!task?.id || running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await postJson(`/api/tasks/${task.id}/clarify`, { vendor_name: vendorName });
      const seen = new Set(events.map((item) => `${item.step}-${item.created_at}-${item.outcome}`));
      const added = (result.audit || []).filter((item) => !seen.has(`${item.step}-${item.created_at}-${item.outcome}`));
      setEvents((current) => [...current, ...added]);
      setTask(result);
      if (result.status !== "waiting_for_user") {
        const continued = await postJson("/api/chat/resume", { task_id: result.id });
        if (continued.reply) {
          setMessages((current) => [...current, { role: "agent", text: continued.reply }]);
        }
      }
    } catch (exc) {
      setError(exc.message);
    } finally {
      running.current = false;
      setBusy(false);
    }
  }

  async function openInvoice(id) {
    if (busy) return;
    const gen = ++replayGen.current;
    setError("");
    setView("chat");
    setNarrating(true);
    setTask(null);
    setEvents([]);
    setSelected(null);
    setMessages([]);
    setDraft("");
    try {
      const invoice = await getJson(`/api/invoices/${id}`);
      if (replayGen.current !== gen) return;
      setSelected(invoice);
      setGridOrigin(withoutLedgerRow(ledger, invoice.invoice_number));
      setInstruction(invoice.task?.instruction || INSTRUCTION);
      setThread({
        source: "invoice",
        instruction: invoice.task?.instruction || invoice.explanation || "",
        filename: invoice.task?.filename || `${invoice.invoice_number}.pdf`,
        previewUrl: `/api/invoices/${id}/pdf`,
      });
      const audit = invoice.task?.audit || [];
      for (const step of audit) {
        if (replayGen.current !== gen) return;
        await wait(90);
        if (replayGen.current !== gen) return;
        setEvents((current) => [...current, step]);
      }
      if (replayGen.current !== gen) return;
      setTask(invoice.task);
    } catch (exc) {
      if (replayGen.current === gen) setError(exc.message);
    } finally {
      if (replayGen.current === gen) setNarrating(false);
    }
  }

  async function approveSheet(invoiceId) {
    setError("");
    try {
      await postJson("/api/ledger/approve", { invoice_id: invoiceId });
      await refresh();
    } catch (exc) {
      setError(exc.message);
    }
  }

  async function review(id, action) {
    setError("");
    try {
      await postJson(`/api/invoices/${id}/${action}`);
      const invoice = await getJson(`/api/invoices/${id}`);
      setSelected(invoice);
      const outcome = action === "approve" ? "approved" : "rejected";
      setEvents((current) => {
        if (current.some((item) => item.step === "human_review" && item.outcome === outcome)) return current;
        return [
          ...current,
          {
            step: "human_review",
            outcome,
            detail: { invoice_id: id },
            created_at: new Date().toISOString(),
          },
        ];
      });
      await refresh();
    } catch (exc) {
      setError(exc.message);
    }
  }

  const waiting = invoices.filter((row) => row.status === "needs_review" || row.status === "pending_approval");
  const listed = queueOnly ? waiting : invoices;

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand-block">
          <h1>Ledger</h1>
          <p>Accounts agent</p>
        </div>
        <button className="rail-main" type="button" onClick={resetThread} disabled={busy}>
          New invoice
        </button>
        <button className="rail-reset" type="button" onClick={resetDemo} disabled={busy}>
          Reset
        </button>
        <div className="rail-tools">
          <button type="button" className={queueOnly ? "on" : ""} onClick={() => setQueueOnly((value) => !value)} disabled={busy}>
            Waiting {waiting.length}
          </button>
          <button type="button" className={view === "books" ? "on" : ""} onClick={() => setView(view === "books" ? "chat" : "books")} disabled={busy}>
            Books
          </button>
          <button type="button" className={view === "ledger" ? "on" : ""} onClick={() => setView(view === "ledger" ? "chat" : "ledger")} disabled={busy}>
            Ledger
          </button>
          <button type="button" className={view === "invoices" ? "on" : ""} onClick={() => setView(view === "invoices" ? "chat" : "invoices")} disabled={busy}>
            Invoices
          </button>
        </div>
        <div className="invoice-list">
          {sheetRows.filter((row) => row.Status === "NEEDS_APPROVAL").map((row) => (
            <div className="sheet-wait" key={row["Invoice ID"]}>
              <span className="invoice-number">{row["Invoice ID"]}</span>
              <span className="invoice-vendor">{row.Vendor}</span>
              <button className="primary" type="button" onClick={() => approveSheet(row["Invoice ID"])} disabled={busy}>
                Approve
              </button>
            </div>
          ))}
          {listed.length ? (
            listed.map((row) => (
              <button
                key={row.id}
                type="button"
                className={selected?.id === row.id ? "on" : ""}
                onClick={() => openInvoice(row.id)}
                disabled={busy}
              >
                <span className="invoice-number">{row.invoice_number}</span>
                <span className="invoice-vendor">{row.vendor_name}</span>
                <span className="badge legacy">Legacy</span>
                <span className={`badge ${row.status}`}>{statusLabel(row.status)}</span>
              </button>
            ))
          ) : (
            <p className="empty">{queueOnly ? "Nothing is waiting on you." : "No invoices yet."}</p>
          )}
        </div>
      </aside>
      <section className="stage">
        <header className={`presence ${busy || narrating ? "working" : ""}`}>
          <div className="presence-inner">
            <span className="mark" aria-hidden="true">L</span>
            <div>
              <strong>Ledger</strong>
              <p aria-live="polite">{view === "books" ? "Purchase orders and goods receipts." : view === "ledger" ? "The SQL ledger." : view === "invoices" ? "Invoice PDFs." : presenceLine(task, busy, events, thread, narrating)}</p>
            </div>
            {view === "books" || view === "ledger" || view === "invoices" ? (
              <button type="button" className="ghost return-chat" onClick={() => setView("chat")} aria-label="Return to the invoice chat">
                Return
              </button>
            ) : null}
          </div>
        </header>
        {view === "books" ? (
          <Books orders={orders} receipts={receipts} />
        ) : view === "ledger" ? (
          <LedgerGrid sheets={ledger} events={[]} live={false} />
        ) : view === "invoices" ? (
          <InvoiceLibrary
            docs={invoiceDocuments(ledger, invoices, tasks, samples)}
            selected={pdfChoice}
            onOpen={(doc) => {
              setPdfChoice((current) => {
                if (current?.url?.startsWith("blob:") && current.url !== doc.url) URL.revokeObjectURL(current.url);
                return doc;
              });
            }}
            onAdd={(list) => addLocalFiles(list, true)}
          />
        ) : (
          <>
            <div className="thread" ref={threadRef}>
              <div className="thread-inner">
                {!thread && !messages.length && !busy ? (
                  <p className="idle">Add one or more invoices, or ask what I can do. With no file, I will tell you what I can access and where the work stands.</p>
                ) : null}
                {messages[0] ? <ChatTurn message={messages[0]} /> : null}
                {thread && (events.length || busy || task) ? (
                  <RunView
                    thread={thread}
                    events={events}
                    busy={busy || narrating}
                    task={task}
                    selected={selected}
                    showResult={!busy && !narrating}
                    onReview={review}
                    onClarify={clarify}
                    ledger={ledger}
                    origin={gridOrigin}
                  />
                ) : null}
                {messages.slice(1).map((message, index) => (
                  <ChatTurn key={`${message.role}-${index + 1}`} message={message} />
                ))}
                {error ? <p className="error" role="alert">{error}</p> : null}
              </div>
            </div>
            <div className="composer">
              <form className="composer-inner" onSubmit={onSend}>
                <label htmlFor="message">Message</label>
                <textarea
                  id="message"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder="Ask what I can do, or tell me to look after the attached invoices."
                />
                <div className="composer-row">
                  <label className="choose" htmlFor="invoice">
                    Invoices
                    <select
                      id="invoice"
                      value=""
                      onChange={(event) => {
                        const name = event.target.value;
                        event.target.value = "";
                        if (name) addSample(name);
                      }}
                      disabled={busy || picking}
                    >
                      <option value="">Add an invoice</option>
                      {samples.map((sample) => (
                        <option key={sample.name} value={sample.name}>
                          {sample.invoice_number || sample.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="choose file-pick">
                    From computer
                    <input
                      type="file"
                      accept="application/pdf,.pdf"
                      multiple
                      onChange={(event) => {
                        addLocalFiles(event.target.files, false);
                        event.target.value = "";
                      }}
                      disabled={busy}
                    />
                  </label>
                  <button className="primary" type="submit" disabled={busy || picking || !draft.trim()}>
                    {busy ? "Working" : "Send"}
                  </button>
                </div>
                {picking ? <p className="attached">Fetching the sample.</p> : null}
                {files.length ? (
                  <div className="chips">
                    {files.map((item) => (
                      <span className="chip" key={item.name}>
                        {item.name}
                        <button type="button" onClick={() => removeFile(item.name)} disabled={busy} aria-label={`Remove ${item.name}`}>
                          Remove
                        </button>
                      </span>
                    ))}
                  </div>
                ) : null}
              </form>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function RunView({ thread, events, busy, task, selected, showResult, onReview, onClarify, ledger, origin }) {
  const thought = runThinking(events);
  const computer = events.filter((event) => event.step === "cua_act" || event.step === "cua_recover" || event.step === "cua_verify" || event.step === "clarification" || event.step === "control_handoff" || event.step === "sheet_adapted");
  const shot = [...computer].reverse().find((event) => event.detail?.screenshot);
  const waiting = task?.status === "waiting_for_user";
  const candidates = [...computer].reverse().find((event) => event.outcome === "waiting")?.detail?.candidates || [];
  return (
    <article className="run">
      {thread.source === "live" ? null : (
        <>
          <p className="who">You</p>
          <p className="you-text">{thread.instruction}</p>
        </>
      )}
      {thread.filename ? <p className="file-line">{thread.filename}</p> : null}
      <ThinkingBlock title={busy ? "Thinking" : "Thought"} text={thought} open={busy} />
      <GoalPlan events={events} />
      <p className="who">Ledger</p>
      <LedgerGrid sheets={ledger} origin={origin} events={events} live={busy} />
      <LiveDesk events={events} />
      {computer.length ? (
        <>
          {shot ? (
            <div className="live-shot">
              <p className="live-line">{shot.detail?.goal || speak(shot)}</p>
              <img className="cua-shot" alt="" src={shot.detail.screenshot} />
            </div>
          ) : null}
          <ol className="plan">
            {computer.map((event, index) => (
              <li key={`${event.step}-${index}`} className={stepClass(event)}>
                <details open={event.step === "cua_verify" || event.outcome === "waiting"}>
                  <summary>
                    <span className="tick" aria-hidden="true" />
                    <span>{event.detail?.goal || speak(event)}</span>
                    <em>{event.outcome === "about_to" ? "Doing this" : (event.detail?.status || event.outcome)}</em>
                  </summary>
                  <EvidenceList event={event} />
                  {event.detail?.screenshot && event !== shot ? <img className="cua-shot" alt="" src={event.detail.screenshot} /> : null}
                </details>
              </li>
            ))}
          </ol>
        </>
      ) : null}
      {showResult && waiting ? (
        <form className="clarify" onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          onClarify(String(data.get("vendor") || ""));
        }}>
          <p>Which vendor should I use?</p>
          <select name="vendor" aria-label="Vendor" defaultValue={candidates[0] || ""}>
            {candidates.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          <button className="primary" type="submit">Use this vendor</button>
        </form>
      ) : null}
      {showResult && closingText(task, selected) ? (
        <p className="result">{closingText(task, selected)} {usageLine(task)}</p>
      ) : null}
      {showResult && selected && needsPerson(selected.status) ? (
        <DecisionCard invoice={selected} />
      ) : null}
    </article>
  );
}

function GoalPlan({ events }) {
  const plan = [...events].reverse().find((event) => event.detail?.plan)?.detail?.plan;
  const handoff = [...events].reverse().find((event) => event.step === "control_handoff");
  if (!plan && !handoff) return null;
  return (
    <>
      <p className="who">Plan</p>
      {plan ? (
        <div className="goal-plan">
          <p>{plan.task_goal}</p>
          <p>{plan.success_condition}</p>
          <p>Done: {(plan.completed || []).join(", ") || "Nothing yet."}</p>
          <p>Current: {plan.current_goal || "Waiting."}</p>
          <p>Pending: {(plan.pending || []).join(", ") || "None."}</p>
          {shownControl(plan.control) ? <p>Control: {shownControl(plan.control)}</p> : null}
          {plan.blocked_on ? <p>Blocked: {plan.blocked_on}</p> : null}
          {plan.evidence && Object.entries(plan.evidence).length ? (
            <ul>
              {Object.entries(plan.evidence).map(([goal, item]) => (
                <li key={goal}>{goal}: {item.decision} {item.evidence}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {handoff ? <p className="result">{handoff.detail?.goal}</p> : null}
    </>
  );
}

function stepClass(event) {
  if (event.step === "cua_act" || event.step === "cua_recover") {
    if ((event.outcome === "ok" || event.outcome === "recovered") && event.detail?.ok === true) return "done";
    if (event.outcome === "rejected") return "pending";
    return "active";
  }
  if (event.outcome === "about_to" || event.detail?.ok === false) return "active";
  return "done";
}

function EvidenceList({ event }) {
  const rows = evidenceRows(event);
  if (!rows.length) return null;
  return (
    <dl className="facts">
      {rows.map((row) => (
        <div key={row[0]}>
          <dt>{row[0]}</dt>
          <dd>{row[1]}</dd>
        </div>
      ))}
    </dl>
  );
}

function evidenceRows(event) {
  const detail = event.detail || {};
  const rows = [];
  const control = event.step === "control_handoff" ? "recovery" : shownControl(detail.control || detail.plan?.control);
  if (control) rows.push(["Control", control]);
  if ((event.step === "cua_act" || event.step === "cua_recover") && detail.action) {
    rows.push(["Action", detail.action]);
  }
  if (postActionObservation(event)) rows.push(["Observation", detail.observed]);
  if (detail.validation === "accepted" || detail.validation === "rejected") {
    rows.push(["Validation", detail.gate_reason ? `${detail.validation}: ${detail.gate_reason}` : detail.validation]);
  }
  const recovery = recoveryLine(event);
  if (recovery) rows.push(["Recovery", recovery]);
  if (event.step === "cua_verify" && (event.outcome === "verified" || event.outcome === "unverified")) {
    const mismatches = detail.mismatches || [];
    rows.push(["Verification", mismatches.length ? `${event.outcome}: ${mismatches.join(" ")}` : event.outcome]);
  }
  return rows;
}

function postActionObservation(event) {
  if (event.outcome === "about_to" || event.outcome === "rejected") return false;
  if (event.step !== "cua_act" && event.step !== "cua_recover") return false;
  return Boolean(event.detail?.observed);
}

function shownControl(control) {
  if (control === "model") return "model";
  if (control === "deterministic") return "deterministic";
  if (control === "deterministic_recovery" || control === "recovery") return "recovery";
  return "";
}

function recoveryLine(event) {
  if (event.step === "control_handoff") return "handoff";
  if (event.step === "sheet_adapted") {
    return event.detail?.label ? `adaptation: ${event.detail.label}` : "adaptation";
  }
  if (event.detail?.recovery) return `retry: ${event.detail.recovery}`;
  return "";
}

function FactList({ event }) {
  const rows = facts(event);
  if (!rows.length) return null;
  return (
    <dl className="facts">
      {rows.map((row, index) => (
        <div key={`${row[0]}-${index}`}>
          <dt>{row[0]}</dt>
          <dd>{row[1]}</dd>
        </div>
      ))}
    </dl>
  );
}

function ThinkingBlock({ title, text, open }) {
  if (!text) return null;
  return (
    <details className="thinking" open={open}>
      <summary>{title}</summary>
      <p>{text}</p>
    </details>
  );
}

function ChatTurn({ message }) {
  if (message.role === "user") {
    return (
      <article className="chat-turn">
        <p className="who">You</p>
        <p className="you-text">{message.text}</p>
      </article>
    );
  }
  return (
    <article className="chat-turn">
      <p className="who">Ledger</p>
      {message.thinking ? <ThinkingBlock title="Thought" text={message.thinking.replaceAll("**", "")} open={false} /> : null}
      <p className="result">{message.text}</p>
    </article>
  );
}

function runThinking(events) {
  const parts = [
    "I'll read the file, ask Gemini for the fields, check those fields, match the purchase order and the goods receipt, and post only if the instruction allows it.",
  ];
  for (const event of events) {
    const text = String(event.detail?.thinking || "").trim().replaceAll("**", "");
    if (text) parts.push(text);
  }
  return parts.join("\n\n");
}

function DecisionCard({ invoice }) {
  return (
    <section className="decision">
      <h2>Legacy record</h2>
      <p>
        {invoice.invoice_number} from {invoice.vendor_name} is a legacy SQLite invoice. Total {invoice.total}. Status {statusLabel(invoice.status)}. Approval of the live ledger happens on the grid.
      </p>
    </section>
  );
}

function Books({ orders, receipts }) {
  return (
    <div className="books">
      <section>
        <h2>Purchase orders</h2>
        {orders.map((order) => (
          <article key={order.po_number} className="book">
            <h3>{order.po_number}</h3>
            <p>Vendor {order.vendor_id}. Tax {order.tax_rate}%.</p>
            <ul>
              {order.lines.map((line) => (
                <li key={line.sku}>
                  {line.sku}: {line.quantity} at {line.unit_price}
                </li>
              ))}
            </ul>
          </article>
        ))}
      </section>
      <section>
        <h2>Goods receipts</h2>
        {receipts.map((receipt) => (
          <article key={receipt.grn_number} className="book">
            <h3>{receipt.grn_number}</h3>
            <p>Against {receipt.po_number}.</p>
            <ul>
              {receipt.lines.map((line) => (
                <li key={line.sku}>
                  {line.sku}: received {line.quantity}
                </li>
              ))}
            </ul>
          </article>
        ))}
      </section>
    </div>
  );
}

function presenceLine(task, busy, events, thread, narrating) {
  if (busy || narrating) return pendingLine(events);
  if (!thread) return "Ready for an invoice.";
  if (!task) return "Opening that invoice.";
  if (task.status === "completed") return "Posted for approval. I checked the saved row.";
  if (task.status === "needs_review") return "Held for review. Nothing was approved.";
  if (task.status === "missing_information") return "Stopped. The invoice is missing something I can trust.";
  if (task.status === "failed") return "Stopped. The task did not finish cleanly.";
  return statusLabel(task.status);
}

function pendingLine(events) {
  const last = events[events.length - 1];
  if (!last) return "Taking the invoice.";
  if (last.step === "received") return "Opening the PDF.";
  if (last.step === "parse_document") return "Asking Gemini to read the page.";
  if (last.step === "extract_fields" && last.outcome === "retry") return "Reading the page again.";
  if (last.step === "extract_fields") return "Checking the fields Gemini returned.";
  if (last.step === "validate_fields") return "Matching the purchase order and the goods receipt.";
  if (last.step === "match_records") return "Choosing whether this can be posted.";
  if (last.step === "decide") return "Writing the invoice, if the rules allow it.";
  if (last.step === "create_invoice") return "Reading the saved row back.";
  if (last.step === "cua_act" || last.step === "cua_recover") return last.detail?.goal || "Working in the ledger.";
  if (last.step === "cua_verify") return "Reading the ledger back.";
  if (last.step === "open_mail" || last.step === "type_mail") return "Writing the email.";
  if (last.step === "send_mail") return "The email went out.";
  if (last.step === "open_slack" || last.step === "type_slack") return "Writing in Slack.";
  if (last.step === "post_slack") return "Posted in Slack.";
  if (last.step === "clarification") return "Waiting for you to choose the vendor.";
  return "Closing the task.";
}

function statusLabel(status) {
  const names = {
    pending_approval: "Needs approval",
    needs_review: "Needs review",
    approved: "Approved",
    rejected: "Rejected",
    missing_information: "Missing detail",
    completed: "Posted",
    failed: "Failed",
    running: "Working",
    waiting_for_user: "Waiting for you",
  };
  return names[status] || status || "Unknown";
}

function speak(event) {
  const detail = event.detail || {};
  if (event.step === "received") return "I have the invoice. I'll start with the file itself.";
  if (event.step === "parse_document" && event.outcome === "poor") {
    return `The page text is too thin to trust. OCR confidence ${shown(detail.ocr_confidence)}.`;
  }
  if (event.step === "parse_document") {
    const count = Number(detail.pages);
    const noun = count === 1 ? "page" : "pages";
    const read = `I read ${shown(detail.characters)} characters from ${shown(detail.pages)} ${noun}.`;
    if (event.outcome === "text") return `${read} The page has a text layer.`;
    if (event.outcome === "ocr") return `${read} I used OCR.`;
    return `${read} The text quality is ${event.outcome}.`;
  }
  if (event.step === "extract_fields" && event.outcome === "retry") return "The first read was too weak. I'm reading the page again.";
  if (event.step === "extract_fields" && event.outcome === "request_clarification") {
    return "The second read still failed. I need a clearer copy before I post anything.";
  }
  if (event.step === "extract_fields" && event.outcome === "failed") {
    return detail.error || "Gemini did not return fields I can use.";
  }
  if (event.step === "extract_fields" && event.outcome === "extracted") {
    const fields = detail.snapshot || {};
    return `Gemini read this as ${fields.vendor_name || "an unknown vendor"}, invoice ${fields.invoice_number || "missing"}, total ${fields.total || "missing"}.`;
  }
  if (event.step === "validate_fields" && event.outcome === "ok") return "The required fields are present and the amounts are numeric.";
  if (event.step === "validate_fields") return `I'm stopping. Missing ${(detail.missing || []).join(", ") || "required fields"}.`;
  if (event.step === "match_records") {
    const issues = detail.issues || [];
    return issues.length
      ? `I checked the books. Issues: ${issues.join(", ")}.`
      : `I checked the books. Vendor ${shown(detail.vendor_id)} matches.`;
  }
  if (event.step === "decide" && detail.overridden) {
    return `Gemini suggested ${shown(detail.model_action)}. I did not follow that. The rules chose ${shown(detail.final_action)}.`;
  }
  if (event.step === "decide") return `The rules chose ${shown(detail.final_action)}.`;
  if (event.step === "create_invoice" && event.outcome === "created") return `I saved invoice ${shown(detail.invoice_id)} as ${shown(detail.status)}.`;
  if (event.step === "create_invoice" && event.outcome === "existing") {
    return `Invoice ${shown(detail.invoice_id)} was already saved. I did not write a second row.`;
  }
  if (event.step === "create_invoice" && event.outcome === "skipped") return `I did not write a row. The action was ${shown(detail.reason)}.`;
  if (event.step === "create_invoice" && event.outcome === "duplicate") {
    return `I stopped. Invoice ${shown(detail.existing_id)} is already in the books.`;
  }
  if (event.step === "create_invoice" && event.outcome === "save_failed") return detail.error || "The save failed.";
  if (event.step === "create_invoice" && event.outcome === "response_lost") {
    return `The save succeeded for invoice ${shown(detail.invoice_id)}, then the response was lost.`;
  }
  if (event.step === "verify_persisted" && event.outcome === "passed") return "I read the row back. It matches what I intended to store.";
  if (event.step === "verify_persisted") return `I read the row back and it does not match. ${(detail.mismatches || []).join(", ")}.`;
  if (event.step === "cua_verify" && event.outcome === "verified") return "I read the ledger back. The saved row matches.";
  if (event.step === "cua_verify") return `The ledger does not match. ${(event.detail?.mismatches || []).join(" ")}`;
  if (event.step === "clarification" && event.outcome === "waiting") return "I need you to choose the vendor before I write anything.";
  if (event.step === "clarification") return `You chose ${event.detail?.vendor_name || "a vendor"}. I am checking the rules again.`;
  if (event.step === "control_handoff") return event.detail?.goal || "Model action rejected twice. Switching to deterministic recovery. Financial rules remain enforced.";
  if (event.step === "sheet_adapted") return `Adapted to the sheet ${event.detail?.label || "that matched"}. ${event.detail?.evidence || ""}`.trim();
  if (event.step === "cua_act" || event.step === "cua_recover") return event.detail?.goal || "Working in the ledger.";
  if (event.step === "type_mail") return `Typing the email to ${event.detail?.to || "the recipient"}.`;
  if (event.step === "send_mail") return `Sent the email to ${event.detail?.to || "the recipient"}.`;
  if (event.step === "type_slack") return `Typing in Slack #${event.detail?.channel || "general"}.`;
  if (event.step === "post_slack") return `Posted in Slack #${event.detail?.channel || "general"}.`;
  if (event.step === "human_review" && event.outcome === "approved") return "You approved this invoice.";
  if (event.step === "human_review" && event.outcome === "rejected") return "You rejected this invoice.";
  return event.outcome || "Done.";
}

function facts(event) {
  const detail = event.detail || {};
  const rows = [["Outcome", shown(event.outcome)]];
  if (event.step === "received") push(rows, "Instruction", detail.instruction);
  if (event.step === "parse_document") {
    push(rows, "Pages", detail.pages);
    push(rows, "Characters", detail.characters);
    push(rows, "Used OCR", detail.used_ocr);
    push(rows, "OCR confidence", detail.ocr_confidence);
  }
  if (event.step === "extract_fields") {
    push(rows, "Model", detail.model);
    push(rows, "Proposed action", detail.proposed_action);
    push(rows, "Note", detail.model_note);
    if (detail.thoughts_tokens) push(rows, "Thought tokens", detail.thoughts_tokens);
    if (detail.input_tokens) push(rows, "Input tokens", detail.input_tokens);
    if (detail.output_tokens) push(rows, "Output tokens", detail.output_tokens);
    if (detail.estimated_cost_usd) push(rows, "Estimated cost USD", detail.estimated_cost_usd);
    push(rows, "Error", detail.error);
    push(rows, "Reason", detail.reason);
    const snapshot = detail.snapshot || {};
    const labels = [
      ["Vendor", "vendor_name"],
      ["Tax ID", "vendor_tax_id"],
      ["Invoice number", "invoice_number"],
      ["Purchase order", "po_number"],
      ["Goods receipt", "grn_number"],
      ["First line quantity", "quantity"],
      ["First line unit price", "unit_price"],
      ["Tax", "tax_amount"],
      ["Total", "total"],
    ];
    for (const [label, key] of labels) {
      if (key in snapshot) push(rows, label, snapshot[key]);
    }
    for (const item of detail.evidence || []) {
      rows.push([item.field ? `Quote: ${item.field}` : "Quote", shown(item.quote)]);
    }
  }
  if (event.step === "validate_fields") {
    const missing = detail.missing || [];
    rows.push(["Missing", missing.length ? missing.join(", ") : "none"]);
  }
  if (event.step === "match_records") {
    push(rows, "Vendor id", detail.vendor_id);
    const issues = detail.issues || [];
    rows.push(["Issues", issues.length ? issues.join(", ") : "none"]);
  }
  if (event.step === "decide") {
    push(rows, "Model action", detail.model_action);
    push(rows, "Final action", detail.final_action);
    push(rows, "Overridden", detail.overridden);
  }
  if (event.step === "create_invoice") {
    push(rows, "Invoice id", detail.invoice_id);
    push(rows, "Status", detail.status);
    push(rows, "Reason", detail.reason);
    push(rows, "Existing id", detail.existing_id);
    push(rows, "Error", detail.error);
  }
  if (event.step === "verify_persisted") {
    push(rows, "Observed id", detail.observed_id);
    push(rows, "Observed status", detail.observed_status);
    push(rows, "Expected a row", detail.expect_row);
    const mismatches = detail.mismatches || [];
    rows.push(["Mismatches", mismatches.length ? mismatches.join(", ") : "none"]);
  }
  if (event.step === "human_review") push(rows, "Invoice id", detail.invoice_id);
  return rows;
}

function push(rows, label, value) {
  if (value === undefined || value === null || value === "") return;
  rows.push([label, shown(value)]);
}

function shown(value) {
  if (value === null || value === undefined || value === "") return "missing";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function closingText(task, selected) {
  return task?.explanation || selected?.explanation || "";
}

function usageLine(task) {
  const usage = task?.usage;
  if (!usage || (!usage.input_tokens && !usage.output_tokens)) return "";
  return `Gemini used ${usage.input_tokens} input tokens and ${usage.output_tokens} output tokens.`;
}

function needsPerson(status) {
  return status === "pending_approval" || status === "needs_review";
}

async function loadJson(path, fallback) {
  try {
    return { value: await getJson(path), error: "" };
  } catch (exc) {
    return { value: fallback, error: exc.message };
  }
}

function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function LedgerGrid({ sheets, origin, events, live }) {
  const gestures = useMemo(() => actionGestures(events), [events]);
  const [index, setIndex] = useState(0);
  const [phase, setPhase] = useState("move");
  const [typed, setTyped] = useState(0);
  const [picked, setPicked] = useState("Invoice_Ledger");
  const [pointer, setPointer] = useState(null);
  const gridRef = useRef(null);
  const playing = gestures.length > 0 && index < gestures.length;
  const painted = playing
    ? paintPlayed(origin || sheets, gestures, index, phase === "move" ? null : { phase, typed })
    : { sheets: sheets || {}, active: picked, filter: "" };
  const names = Object.keys(painted.sheets);
  const active = playing ? painted.active : picked;
  const sheet = painted.sheets[active] || painted.sheets.Invoice_Ledger || { columns: [], rows: [] };
  const current = playing ? gestures[index] : null;
  const typingCell = current && phase === "type" && current.action === "edit_cell" ? `${current.row}:${current.column}` : "";

  useEffect(() => {
    if (gestures.length) return;
    setIndex(0);
    setPhase("move");
    setTyped(0);
    setPointer(null);
  }, [gestures.length]);

  useEffect(() => {
    if (!playing) return undefined;
    const gesture = gestures[index];
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (phase === "move") {
      const timer = setTimeout(() => setPhase(gesture.text ? "type" : "click"), reduce ? 40 : 420);
      return () => clearTimeout(timer);
    }
    if (phase === "type") {
      if (!reduce && typed < gesture.text.length) {
        const timer = setTimeout(() => setTyped((value) => value + 1), 28);
        return () => clearTimeout(timer);
      }
      const timer = setTimeout(() => {
        setIndex((value) => value + 1);
        setTyped(0);
        setPhase("move");
      }, reduce ? 40 : 200);
      return () => clearTimeout(timer);
    }
    const timer = setTimeout(() => {
      setIndex((value) => value + 1);
      setTyped(0);
      setPhase("move");
    }, reduce ? 40 : 280);
    return () => clearTimeout(timer);
  }, [playing, index, phase, typed, gestures]);

  useLayoutEffect(() => {
    const gesture = gestures[index];
    const root = gridRef.current;
    if (!playing || !gesture || !root) return;
    const target = root.querySelector(`[data-agent="${agentTarget(gesture)}"]`);
    if (!target) {
      setPointer({ x: 24, y: 28, goal: gesture.goal });
      return;
    }
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
    const rootBox = root.getBoundingClientRect();
    const box = target.getBoundingClientRect();
    setPointer({
      x: box.left - rootBox.left + Math.min(16, box.width * 0.3),
      y: box.top - rootBox.top + box.height * 0.45,
      goal: gesture.goal,
    });
  }, [playing, index, phase, typed, gestures, active, painted.filter, sheet.rows.length]);

  return (
    <div className={playing || live ? "ledger-grid agent-live" : "ledger-grid"} ref={gridRef}>
      <div className="ledger-tabs">
        {names.map((name) => (
          <button
            key={name}
            type="button"
            data-agent={`tab:${name}`}
            className={name === active ? "on" : ""}
            onClick={() => setPicked(name)}
          >
            {name.replaceAll("_", " ")}
          </button>
        ))}
      </div>
      {playing ? (
        <div className="ledger-toolbar">
          <span className="filter-box" data-agent="filter">
            {painted.filter}
            {current?.action === "search_sheet" && phase === "type" ? <i className="caret" /> : null}
          </span>
          <span className="agent-button" data-agent="add">Add row</span>
          <span className="agent-button" data-agent="save">Save</span>
          <span className="agent-button" data-agent="open">Ledger</span>
        </div>
      ) : null}
      <div className="ledger-scroll">
        <table>
          <thead>
            <tr>
              {(sheet.columns || []).map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(sheet.rows || []).map((row, rowIndex) => (
              <tr key={`${active}-${rowIndex}`}>
                {(sheet.columns || []).map((column) => {
                  const marked = typingCell === `${rowIndex}:${column}`;
                  return (
                    <td key={column} data-agent={`cell:${rowIndex}:${column}`} className={marked ? "agent-cell" : ""}>
                      {row[column] || ""}
                      {marked ? <i className="caret" /> : null}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <AgentPointer pointer={playing ? pointer : null} pressing={phase === "click" || (phase === "type" && typed === 0)} />
    </div>
  );
}

function AgentPointer({ pointer, pressing }) {
  if (!pointer) return null;
  return (
    <div className={pressing ? "agent-pointer pressing" : "agent-pointer"} style={{ left: pointer.x, top: pointer.y }}>
      <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
        <path d="M2 1.5 L2 14.5 L6.2 10.6 L9.2 16.2 L11.4 15.1 L8.4 9.6 L14 9.2 Z" fill="#16382c" stroke="#fff" strokeWidth="1" />
      </svg>
      <span>{pointer.goal}</span>
    </div>
  );
}

function agentTarget(gesture) {
  if (gesture.action === "select_sheet") return `tab:${gesture.sheet}`;
  if (gesture.action === "search_sheet") return "filter";
  if (gesture.action === "add_row") return "add";
  if (gesture.action === "save_workbook") return "save";
  if (gesture.action === "edit_cell") return `cell:${gesture.row}:${gesture.column}`;
  return "open";
}

function actionGestures(events) {
  const gestures = [];
  for (const event of events || []) {
    if (event.step !== "cua_act") continue;
    if (event.outcome !== "ok" && event.outcome !== "recovered") continue;
    const detail = event.detail || {};
    const action = detail.action || "";
    let text = "";
    if (action === "edit_cell") text = String(detail.value ?? "");
    if (action === "search_sheet") text = String(detail.search || filterText(detail.goal));
    gestures.push({
      action,
      sheet: detail.sheet || "Invoice_Ledger",
      row: Number.isInteger(detail.row) ? detail.row : null,
      column: detail.column || "",
      value: String(detail.value ?? ""),
      text,
      goal: detail.goal || "Working in the ledger.",
    });
  }
  return gestures;
}

function filterText(goal) {
  const text = String(goal || "");
  const marker = "Filtering for ";
  if (!text.startsWith(marker)) return "";
  return text.slice(marker.length).replace(/\.$/, "");
}

function cloneSheets(sheets) {
  const next = {};
  for (const [name, sheet] of Object.entries(sheets || {})) {
    next[name] = {
      columns: sheet.columns || [],
      rows: (sheet.rows || []).map((row) => ({ ...row })),
    };
  }
  if (!next.Invoice_Ledger) next.Invoice_Ledger = { columns: [], rows: [] };
  return next;
}

function pushBlank(sheet) {
  const blank = {};
  for (const column of sheet.columns) blank[column] = "";
  sheet.rows.push(blank);
}

function paintPlayed(baseSheets, gestures, index, liveGesture) {
  const next = cloneSheets(baseSheets);
  let active = "Invoice_Ledger";
  let filter = "";
  const apply = (gesture, live) => {
    if (gesture.action === "select_sheet" && next[gesture.sheet] && !live) active = gesture.sheet;
    if (gesture.action === "search_sheet") {
      filter = live ? gesture.text.slice(0, live.typed) : gesture.text;
      if (gesture.sheet && next[gesture.sheet]) active = gesture.sheet;
    }
    if (gesture.action === "add_row" && !live) {
      pushBlank(next.Invoice_Ledger);
      active = "Invoice_Ledger";
    }
    if (gesture.action === "edit_cell" && gesture.column) {
      const rows = next.Invoice_Ledger.rows;
      let rowIndex = gesture.row;
      if (!Number.isInteger(rowIndex)) rowIndex = Math.max(0, rows.length - 1);
      while (rows.length <= rowIndex) pushBlank(next.Invoice_Ledger);
      const value = live ? gesture.text.slice(0, live.typed) : gesture.value;
      rows[rowIndex] = { ...rows[rowIndex], [gesture.column]: value };
      active = "Invoice_Ledger";
    }
    if (gesture.action === "save_workbook" && !live) active = "Invoice_Ledger";
  };
  for (let cursor = 0; cursor < index; cursor += 1) apply(gestures[cursor], null);
  if (liveGesture && gestures[index]) apply(gestures[index], liveGesture);
  return { sheets: next, active, filter };
}

function withoutLedgerRow(sheets, invoiceNumber) {
  const next = cloneSheets(sheets);
  if (invoiceNumber && next.Invoice_Ledger) {
    next.Invoice_Ledger.rows = next.Invoice_Ledger.rows.filter((row) => row["Invoice ID"] !== invoiceNumber);
  }
  return next;
}

function invoiceDocuments(sheets, invoices, tasks, samples) {
  const sampleNames = new Set((samples || []).map((sample) => sample.name));
  const taskByName = new Map();
  for (const task of tasks || []) {
    if (task.filename) taskByName.set(task.filename, task.id);
  }
  const docs = [];
  const seen = new Set();
  for (const row of sheets?.Invoice_Ledger?.rows || []) {
    const number = row["Invoice ID"] || "";
    const source = row["Source PDF"] || "";
    if (!number && !source) continue;
    const key = number || source;
    if (seen.has(key)) continue;
    seen.add(key);
    let url = "";
    if (source && sampleNames.has(source)) url = `/api/samples/${encodeURIComponent(source)}`;
    else if (source && taskByName.has(source)) url = `/api/tasks/${taskByName.get(source)}/pdf`;
    else if (source) url = `/api/samples/${encodeURIComponent(source)}`;
    docs.push({
      key,
      number: number || source,
      vendor: row.Vendor || "",
      status: row.Status || "",
      source,
      url,
    });
  }
  for (const row of invoices || []) {
    if (seen.has(row.invoice_number)) continue;
    docs.push({
      key: `legacy-${row.id}`,
      number: row.invoice_number,
      vendor: row.vendor_name,
      status: statusLabel(row.status),
      source: "Legacy",
      url: `/api/invoices/${row.id}/pdf`,
    });
  }
  return docs;
}

function InvoiceLibrary({ docs, selected, onOpen, onAdd }) {
  return (
    <div className="invoice-library">
      <div className="invoice-library-list">
        <label className="choose file-pick">
          Add from computer
          <input
            type="file"
            accept="application/pdf,.pdf"
            multiple
            onChange={(event) => {
              onAdd(event.target.files);
              event.target.value = "";
            }}
          />
        </label>
        {docs.length ? docs.map((doc) => (
          <button key={doc.key} type="button" className={selected?.key === doc.key ? "on" : ""} onClick={() => onOpen(doc)}>
            <span className="invoice-number">{doc.number}</span>
            <span className="invoice-vendor">{doc.vendor}</span>
            <span className="badge">{doc.status}</span>
          </button>
        )) : <p className="empty">No invoices yet.</p>}
      </div>
      <div className="invoice-pdf">
        {selected?.url ? <iframe title={selected.number} src={selected.url} /> : <p>Choose an invoice to open its PDF.</p>}
      </div>
    </div>
  );
}

function LiveDesk({ events }) {
  const mail = events.some((event) => event.step === "open_mail" || event.step === "type_mail");
  const slack = events.some((event) => event.step === "open_slack" || event.step === "type_slack");
  if (!mail && !slack) return null;
  return (
    <div className="live-desk">
      {mail ? <MailPane events={events} /> : null}
      {slack ? <SlackPane events={events} /> : null}
    </div>
  );
}

function MailPane({ events }) {
  const typed = [...events].reverse().find((event) => event.step === "type_mail");
  const sent = events.some((event) => event.step === "send_mail");
  const detail = typed?.detail || {};
  return (
    <section className="composer-pane">
      <p className="who">Mail</p>
      <p>To {detail.to || ""}</p>
      <p>Subject {detail.subject || ""}</p>
      <TypedText text={detail.body || ""} done={sent} doneLabel="Sent" />
    </section>
  );
}

function SlackPane({ events }) {
  const typed = [...events].reverse().find((event) => event.step === "type_slack");
  const sent = events.some((event) => event.step === "post_slack");
  const detail = typed?.detail || {};
  return (
    <section className="slack-pane">
      <p className="who">Slack #{detail.channel || "general"}</p>
      <TypedText text={detail.body || ""} done={sent} doneLabel="Posted" />
    </section>
  );
}

function TypedText({ text, done, doneLabel }) {
  const [count, setCount] = useState(0);
  useEffect(() => {
    setCount(0);
  }, [text]);
  useEffect(() => {
    if (count >= text.length) return undefined;
    const timer = setTimeout(() => setCount((value) => value + 1), 12);
    return () => clearTimeout(timer);
  }, [count, text]);
  const finished = count >= text.length;
  return (
    <p className="typed">
      {text.slice(0, count)}
      {finished && done ? <em> {doneLabel}</em> : null}
    </p>
  );
}
