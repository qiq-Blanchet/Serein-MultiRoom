import { roomLocalStorage } from "./roomScopedStorage.js";
// Canonical personal records; old browser data is imported without overwriting newer server state.
const records = new Map();
let ready;
const memoryKey = "serein.memory.scene-records.v1";
const reviewKey = "serein.basement.recall-observation-review.v1";
const simulationKey = "serein.basement.recall-simulation-training.v1";
const importKey = "serein.personal.imported.v1";
const read = (key, fallback) => { try { return JSON.parse(roomLocalStorage().getItem(key)) ?? fallback; } catch { return fallback; } };

async function request(path, body) {
  const response = await fetch(`/__serein/personal${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(response.status === 409
    ? "这条记录在另一处更新了，请刷新后再保存。"
    : "暂时没有保存到服务器，请稍后重试。");
  return result;
}

function legacyRecords() {
  const result = [];
  for (const scene of read(memoryKey, [])) {
    const id = scene.canonicalSceneId || scene.id;
    if (scene.sourceKind !== "serein-live-readonly" || !id) continue;
    if (scene.favorite) result.push({ scope: "favorite", key: id, document_id: id, value: { favorite: true } });
    for (const note of scene.annotations || []) {
      if (note.id && note.content) result.push({ scope: "annotation", key: note.id, document_id: id,
        value: { ...note, role: note.role || "user" } });
    }
  }
  for (const [key, value] of Object.entries(read(reviewKey, {}))) result.push({ scope: "recall_review", key, value });
  for (const value of read(simulationKey, {}).labels || []) {
    if (value.id) result.push({ scope: "recall_simulation", key: value.id, value });
  }
  return result;
}

export async function loadPersonalScope(scope) {
  const loaded = [];
  for (let offset = 0; ; ) {
    const page = await request(`?scope=${scope}&offset=${offset}`);
    loaded.push(...page.items);
    if (!page.has_more) break;
    offset = page.next_offset;
  }
  for (const key of records.keys()) if (key.startsWith(`${scope}:`)) records.delete(key);
  for (const row of loaded) records.set(`${scope}:${row.key}`, row);
  return loaded;
}

export function initializePersonal() {
  if (!ready) ready = (async () => {
    if (!roomLocalStorage().getItem(importKey)) {
      const legacy = legacyRecords();
      if (legacy.length) roomLocalStorage().setItem("serein.personal.legacy-backup.v1", JSON.stringify(legacy));
      for (let start = 0; start < legacy.length; start += 200) {
        await request("/import", { records: legacy.slice(start, start + 200) });
      }
      roomLocalStorage().setItem(importKey, "1");
    }
    await Promise.all(["favorite", "annotation", "recall_review", "recall_simulation"].map(loadPersonalScope));
    roomLocalStorage().setItem(reviewKey, JSON.stringify(Object.fromEntries(personalRows("recall_review").map(r => [r.key, r.value]))));
    roomLocalStorage().setItem(simulationKey, JSON.stringify({ schemaVersion: 3, labels: personalRows("recall_simulation").map(r => r.value) }));
    roomLocalStorage().removeItem("serein.memory.live-cache-at.v1");
  })().catch(error => { ready = undefined; throw error; });
  return ready;
}

export const personalRows = scope => [...records.values()].filter(r => r.scope === scope && !r.deleted);

export async function savePersonal(scope, key, value, documentId = null, deleted = false) {
  await initializePersonal();
  const previous = records.get(`${scope}:${key}`);
  const row = await request("", { scope, key, value, document_id: documentId,
    expected_revision: previous?.revision || 0, deleted });
  records.set(`${scope}:${key}`, row);
  roomLocalStorage().removeItem("serein.memory.live-cache-at.v1");
  return row;
}
