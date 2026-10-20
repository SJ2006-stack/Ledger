const SPREADSHEET_ID = "19DpNYaW7I_nleAjqT1XBmA7VnZzUQBfOWNRx0xwlrLk";

function doPost(e) {
  const body = JSON.parse((e.postData && e.postData.contents) || "{}");
  const book = SpreadsheetApp.openById(SPREADSHEET_ID);
  if (body.action === "read") return json_({ok: true, sheets: read_(book)});
  if (body.action === "write") {
    write_(book, body.sheets || {});
    return json_({ok: true, sheets: read_(book)});
  }
  return json_({ok: false, error: "Unknown action."});
}

function read_(book) {
  const out = {};
  book.getSheets().forEach((sheet) => {
    const values = sheet.getDataRange().getDisplayValues();
    out[sheet.getName()] = values;
  });
  return out;
}

function write_(book, sheets) {
  Object.keys(sheets).forEach((name) => {
    let sheet = book.getSheetByName(name);
    if (!sheet) sheet = book.insertSheet(name);
    const rows = sheets[name] || [];
    sheet.clear();
    if (!rows.length || !rows[0].length) return;
    const width = rows[0].length;
    const rectangular = rows.map((row) => {
      const copy = row.slice(0, width).map((cell) => String(cell == null ? "" : cell));
      while (copy.length < width) copy.push("");
      return copy;
    });
    sheet.getRange(1, 1, rectangular.length, width).setValues(rectangular);
  });
  const leftover = book.getSheetByName("Sheet1");
  if (leftover && book.getSheets().length > 1) book.deleteSheet(leftover);
}

function json_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}
