export async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

export async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

export async function streamTask(instruction, file, idempotencyKey, onAudit) {
  return streamForm("/api/tasks/stream", instruction, file, idempotencyKey, onAudit);
}

export async function streamCua(instruction, file, idempotencyKey, onAudit) {
  return streamForm("/api/tasks/cua/stream", instruction, file, idempotencyKey, onAudit);
}

export async function streamChat(message, files, taskId, onFrame) {
  const body = new FormData();
  body.append("message", message);
  body.append("task_id", taskId || "");
  for (const file of files || []) body.append("files", file);
  const response = await fetch("/api/chat/stream", { method: "POST", body });
  if (!response.ok || !response.body) {
    throw new Error(await readError(response));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done = null;
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const parsed = parseFrame(frame);
      if (!parsed) continue;
      if (parsed.event === "audit" || parsed.event === "say") onFrame(parsed);
      if (parsed.event === "done") done = parsed.data;
      if (parsed.event === "error") throw new Error(parsed.data.detail || "The task failed.");
    }
  }
  if (!done) throw new Error("The chat ended before a result.");
  return done;
}

async function streamForm(path, instruction, file, idempotencyKey, onAudit) {
  const body = new FormData();
  body.append("instruction", instruction);
  body.append("idempotency_key", idempotencyKey);
  body.append("file", file);
  const response = await fetch(path, { method: "POST", body });
  if (!response.ok || !response.body) {
    throw new Error(await readError(response));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let task = null;
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const parsed = parseFrame(frame);
      if (!parsed) continue;
      if (parsed.event === "audit") onAudit(parsed.data);
      if (parsed.event === "done") task = parsed.data;
      if (parsed.event === "error") throw new Error(parsed.data.detail || "The task failed.");
    }
  }
  if (!task) throw new Error("The task stream ended before a result.");
  return task;
}

function parseFrame(frame) {
  let event = "message";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}

export async function sampleFile(name) {
  const response = await fetch(`/api/samples/${name}`);
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  const blob = await response.blob();
  return new File([blob], name, { type: "application/pdf" });
}

async function readError(response) {
  try {
    const payload = await response.json();
    return payload.detail || response.statusText;
  } catch {
    return response.statusText;
  }
}
