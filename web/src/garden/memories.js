import { roomLocalStorage } from "../storage/roomScopedStorage.js";
import { makeFlowers } from './geometry.js';

const layoutKey = 'serein.garden.memory-positions.v1';

export function projectMemories(payload) {
  const records = new Map();
  function add(record) {
    if (records.has(record.key)) throw new Error('线上记忆列表正在变化，请重新读取。');
    records.set(record.key, record);
  }
  for (const scene of payload.scenes) {
    if (!scene.source_id || (scene.object_kind && scene.object_kind !== 'scene')) continue;
    add({
      key: `scene:${scene.source_id}`, sourceId: scene.source_id, kind: 'scene',
      title: scene.title || '没有题目的一幕', content: scene.content || '', date: scene.date || '',
      archived: scene.type === 'archived' || scene.scene_status === 'archived'
        || scene.active === false || scene.status === '已沉底',
    });
  }
  for (const event of payload.events) {
    if (event.item_type !== 'event' || event.status !== 'active' || !event.item_id) continue;
    add({
      key: `event:${event.item_id}`, sourceId: event.item_id, kind: 'event',
      title: event.title || '没有题目的一段往事', content: event.body || '',
      date: event.local_date || '', archived: event.status === 'archived',
    });
  }
  // This endpoint supplies the reviewed, currently valid Scene projection.
  // Event similarity, coverage, and titles never create garden relations.
  const relations = new Map();
  for (const edge of payload.edges) {
    if (![true, 1].includes(edge.active) || edge.valid === false
      || (edge.lifecycle_status && edge.lifecycle_status !== 'active')) continue;
    const a = `scene:${edge.source}`, b = `scene:${edge.target}`;
    if (a === b || !records.has(a) || !records.has(b)) continue;
    for (const [from, to] of [[a, b], [b, a]]) {
      if (!relations.has(from)) relations.set(from, new Set());
      relations.get(from).add(to);
    }
  }
  return { records: [...records.values()], relations };
}

function hashKey(key) {
  let value = 2166136261;
  for (const char of key) value = Math.imul(value ^ char.charCodeAt(0), 16777619);
  return value >>> 0;
}

export function assignMemorySlots(records, saved = []) {
  const slots = new Map(saved);
  let next = Math.max(-1, ...slots.values()) + 1;
  // Seeded order is independent of dates, importance, and API pagination.
  const missing = records.filter(record => !slots.has(record.key)).sort((a, b) =>
    hashKey(a.key) - hashKey(b.key) || a.key.localeCompare(b.key));
  for (const record of missing) slots.set(record.key, next++);
  return [...slots];
}

export function memoryLayout(records, storage = roomLocalStorage()) {
  let saved = [];
  try {
    const parsed = JSON.parse(storage.getItem(layoutKey));
    if (Array.isArray(parsed) && parsed.every(entry => Array.isArray(entry)
      && typeof entry[0] === 'string' && Number.isInteger(entry[1]) && entry[1] >= 0)
      && new Set(parsed.map(entry => entry[1])).size === parsed.length) saved = parsed;
  } catch { /* A fresh browser starts from the same seeded order. */ }
  const slots = assignMemorySlots(records, saved);
  try { storage.setItem(layoutKey, JSON.stringify(slots)); } catch { /* Keep the current layout in memory. */ }
  return slots;
}

export function makeMemoryFlowers(records, layout) {
  const slots = new Map(layout);
  const templates = makeFlowers(Math.max(0, ...records.map(record => slots.get(record.key))) + 1);
  return [...records].sort((a, b) => slots.get(a.key) - slots.get(b.key)).map((record, id) => ({
    ...templates[slots.get(record.key)], id, slot: slots.get(record.key), record,
    archived: record.archived,
  }));
}
