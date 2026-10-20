const COLUMNS = {
  Invoice_Ledger: ["Invoice ID", "Vendor", "Tax ID", "Invoice Date", "PO ID", "GRN ID", "Subtotal", "Tax Rate", "Tax Amount", "Total", "Status", "Exception", "Source PDF", "Processed At"],
  Purchase_Orders: ["PO ID", "Vendor", "SKU", "Description", "Ordered Qty", "Unit Price", "Currency"],
  Goods_Receipts: ["GRN ID", "PO ID", "SKU", "Received Qty", "Receipt Date"],
  Vendors: ["Vendor ID", "Legal Name", "Tax ID", "Active", "Payment Terms"],
};

const state = {
  sheets: {},
  active: "Invoice_Ledger",
  selected: { r: 0, c: 0 },
  search: "",
  columnFilters: {},
  dialog: "",
  saveState: "idle",
  fault: "",
  faultUsed: false,
  ready: false,
  activity: "",
};

const status = document.querySelector("#status");
const tabs = document.querySelector("#tabs");
const grid = document.querySelector("#grid");
const formula = document.querySelector("#formula");
const cellRef = document.querySelector("#cell-ref");
const dialog = document.querySelector("#dialog");
const dialogText = document.querySelector("#dialog-text");

function snapshot() {
  const columns = COLUMNS[state.active];
  const rows = [];
  (state.sheets[state.active] || []).forEach((raw, index) => {
    const cells = {};
    columns.forEach((column, position) => {
      cells[column] = String(raw[position] || "");
    });
    rows.push({ index, cells });
  });
  return {
    sheet: state.active,
    dialog: state.dialog,
    saveState: state.saveState,
    searchQuery: state.search,
    columns,
    rows,
    rowCount: (state.sheets[state.active] || []).length,
  };
}

function rowVisible(cells) {
  const query = state.search.trim().toLowerCase();
  if (query && !Object.values(cells).join(" ").toLowerCase().includes(query)) return false;
  return Object.entries(state.columnFilters).every(([column, filter]) => {
    if (!filter) return true;
    return String(cells[column] || "").toLowerCase().includes(filter.toLowerCase());
  });
}

function render() {
  const columns = COLUMNS[state.active];
  tabs.replaceChildren();
  Object.keys(COLUMNS).forEach((name) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.sheetTab = name;
    button.append(document.createTextNode(name.replaceAll("_", " ")));
    if (name === state.active) {
      button.className = "on";
      if (state.activity) {
        const note = document.createElement("span");
        note.className = "tab-live";
        note.textContent = state.activity;
        button.append(note);
      }
    }
    button.addEventListener("click", () => {
      state.active = name;
      state.search = "";
      state.columnFilters = {};
      document.querySelector("#sheet-search").value = "";
      state.selected = { r: 0, c: 0 };
      render();
    });
    tabs.append(button);
  });

  const head = document.createElement("thead");
  const titles = document.createElement("tr");
  const filters = document.createElement("tr");
  columns.forEach((column, index) => {
    const title = document.createElement("th");
    title.textContent = column;
    titles.append(title);
    const filterCell = document.createElement("th");
    const input = document.createElement("input");
    input.className = "col-filter";
    input.ariaLabel = `Filter ${column}`;
    input.value = state.columnFilters[column] || "";
    input.addEventListener("input", () => {
      state.columnFilters[column] = input.value;
      render();
    });
    filterCell.append(input);
    filters.append(filterCell);
  });
  head.append(titles, filters);

  const body = document.createElement("tbody");
  (state.sheets[state.active] || []).forEach((raw, rowIndex) => {
    const cells = {};
    columns.forEach((column, position) => {
      cells[column] = String(raw[position] || "");
    });
    const tr = document.createElement("tr");
    const filtering = state.search.trim() || Object.values(state.columnFilters).some(Boolean);
    if (filtering && rowVisible(cells)) tr.classList.add("hit");
    columns.forEach((column, colIndex) => {
      const td = document.createElement("td");
      td.dataset.row = String(rowIndex);
      td.dataset.col = String(colIndex);
      td.dataset.column = column;
      td.textContent = cells[column];
      if (state.selected.r === rowIndex && state.selected.c === colIndex) td.classList.add("selected");
      td.addEventListener("click", () => selectCell(rowIndex, colIndex));
      td.addEventListener("dblclick", () => formula.focus());
      tr.append(td);
    });
    body.append(tr);
  });
  grid.replaceChildren(head, body);
  grid.querySelector("td.selected")?.scrollIntoView({ block: "nearest" });
  const banner = document.querySelector("#agent-banner");
  if (banner && state.activity) banner.textContent = state.activity;
  const column = columns[state.selected.c] || columns[0];
  cellRef.textContent = column;
  const raw = (state.sheets[state.active] || [])[state.selected.r] || [];
  if (document.activeElement !== formula) formula.value = String(raw[state.selected.c] || "");
  dialog.hidden = !state.dialog;
  dialogText.textContent = dialogMessage(state.dialog);
  status.textContent = state.saveState === "saved"
    ? `Saved. Showing ${state.active.replaceAll("_", " ")}.`
    : `Showing ${state.active.replaceAll("_", " ")}.`;
}

function dialogMessage(name) {
  if (name === "save_error") return "The save did not persist. Nothing new was written.";
  if (name === "lost_response") return "The save may have persisted, but the confirmation was lost.";
  if (name === "other") return "The workbook needs attention before the next edit.";
  return "";
}

function selectCell(row, col) {
  state.selected = { r: row, c: col };
  render();
}

function commitFormula() {
  const columns = COLUMNS[state.active];
  const rows = state.sheets[state.active] || (state.sheets[state.active] = []);
  while (rows.length <= state.selected.r) rows.push(Array(columns.length).fill(""));
  const row = rows[state.selected.r];
  while (row.length < columns.length) row.push("");
  row[state.selected.c] = formula.value;
  state.saveState = "idle";
  render();
}

formula.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    commitFormula();
  }
});

document.querySelector("#sheet-search-go").addEventListener("click", () => {
  state.search = document.querySelector("#sheet-search").value.trim();
  render();
});

document.querySelector("#add-row").addEventListener("click", () => {
  if (state.active !== "Invoice_Ledger") {
    state.dialog = "other";
    render();
    return;
  }
  const columns = COLUMNS.Invoice_Ledger;
  const rows = state.sheets.Invoice_Ledger || (state.sheets.Invoice_Ledger = []);
  rows.push(Array(columns.length).fill(""));
  state.selected = { r: rows.length - 1, c: 0 };
  state.search = "";
  document.querySelector("#sheet-search").value = "";
  render();
});

document.querySelector("#save-book").addEventListener("click", () => {
  saveWorkbook();
});

document.querySelector("#dialog-ok").addEventListener("click", () => {
  state.dialog = "";
  render();
});

document.addEventListener("keydown", (event) => {
  if (event.target === formula || event.target.id === "sheet-search" || event.target.classList.contains("col-filter")) {
    return;
  }
  const columns = COLUMNS[state.active];
  if (event.key === "ArrowRight") state.selected.c = Math.min(columns.length - 1, state.selected.c + 1);
  if (event.key === "ArrowLeft") state.selected.c = Math.max(0, state.selected.c - 1);
  if (event.key === "ArrowDown") state.selected.r += 1;
  if (event.key === "ArrowUp") state.selected.r = Math.max(0, state.selected.r - 1);
  if (event.key.startsWith("Arrow")) {
    event.preventDefault();
    render();
  }
});

async function saveWorkbook() {
  if (state.fault === "save_failure" && !state.faultUsed) {
    state.faultUsed = true;
    state.dialog = "save_error";
    state.saveState = "error";
    render();
    return;
  }
  const response = await fetch("/api/workbook", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sheets: state.sheets }),
  });
  if (!response.ok) {
    state.dialog = "save_error";
    state.saveState = "error";
    render();
    return;
  }
  if (state.fault === "lost_response" && !state.faultUsed) {
    state.faultUsed = true;
    state.dialog = "lost_response";
    state.saveState = "unconfirmed";
    render();
    return;
  }
  state.dialog = "";
  state.saveState = "saved";
  render();
}

async function boot() {
  const params = new URLSearchParams(location.search);
  if (params.get("agent") === "1") {
    const banner = document.querySelector("#agent-banner");
    if (banner) banner.hidden = false;
  }
  const config = await fetch("/api/cua/config").then((response) => response.json());
  const requested = params.get("fault") || "";
  if (config.faults && requested) state.fault = requested;
  const payload = await fetch("/api/workbook").then((response) => response.json());
  state.sheets = payload.sheets;
  Object.keys(COLUMNS).forEach((name) => {
    if (!state.sheets[name]) state.sheets[name] = [];
  });
  state.ready = true;
  render();
}

window.ledgerGrid = {
  get ready() { return state.ready; },
  snapshot,
  setActivity(message) {
    state.activity = String(message || "");
    const banner = document.querySelector("#agent-banner");
    if (banner) {
      banner.hidden = false;
      banner.textContent = state.activity || "Ledger is using this browser.";
    }
    if (state.ready) render();
  },
};

boot().catch(() => {
  status.textContent = "The workbook could not be loaded.";
});
