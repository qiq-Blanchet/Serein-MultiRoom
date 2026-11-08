import { roomLocalStorage } from "../storage/roomScopedStorage.js";
const prefix = 'serein.persona.seen.v1:';
const memory = new Map();

export function readPersonaSeen(scope, storage = roomLocalStorage()) {
  const key = prefix + scope;
  try {
    const value = storage.getItem(key);
    if (value !== null && /^\d+$/.test(value)) return Number(value);
  } catch { /* A private browser can still remember this visit. */ }
  return memory.get(key) ?? null;
}

export function writePersonaSeen(scope, id, storage = roomLocalStorage()) {
  const key = prefix + scope;
  memory.set(key, id);
  try { storage.setItem(key, String(id)); } catch { /* Session-only fallback. */ }
}

export function personaChanges(events, seen) {
  const successful = events.filter(event => !event.error);
  const latest = Math.max(0, ...events.map(event => event.id));
  // The first visit (or a replaced empty database) establishes a baseline.
  const baseline = seen === null || latest < seen;
  return { latest, baseline, unread: baseline ? [] : successful.filter(event => event.id > seen) };
}
