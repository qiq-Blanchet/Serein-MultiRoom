// Per-room browser persistence. Legacy single-room keys are unchanged.
// The room id is learned from the same-origin verified gateway BEFORE the
// Dashboard modules load, so stale room-local UI data is not cross-loaded.
let roomId = null;

export async function initializeRoomScope(fetchImpl = fetch) {
  if (roomId) return roomId;
  const response = await fetchImpl("/__serein/rooms/current", {
    cache: "no-store", credentials: "same-origin",
  });
  // In legacy mode this route is not installed; Vite may return the HTML
  // fallback (200 text/html). Avoid changing any single-room storage keys.
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    if (response.status === 200 || response.status === 404) return null;
    throw Error("Unable to verify current room");
  }
  if (!response.ok) throw Error("Unable to verify current room");
  const body = await response.json();
  if (!body.enabled || !/^[a-f0-9]{32}$/.test(body.room_id || "")) {
    throw Error("Invalid authenticated room context");
  }
  roomId = body.room_id;
  return roomId;
}

export function currentRoomScope() { return roomId; }

function scoped(storage) {
  if (!roomId) return storage;
  // Do not migrate old single-room storage into multiple rooms implicitly.
  const prefix = "serein.multiroom." + roomId + ":";
  return {
    getItem: (key) => storage.getItem(prefix + key),
    setItem: (key, value) => storage.setItem(prefix + key, value),
    removeItem: (key) => storage.removeItem(prefix + key),
    key: (index) => {
      const keys = [];
      for (let i = 0; i < storage.length; i++) {
        const candidate = storage.key(i);
        if (candidate?.startsWith(prefix)) keys.push(candidate.slice(prefix.length));
      }
      return keys[index] ?? null;
    },
    get length() {
      let count = 0;
      for (let i = 0; i < storage.length; i++) {
        if (storage.key(i)?.startsWith(prefix)) count++;
      }
      return count;
    },
    clear: () => {
      const remove = [];
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        if (key?.startsWith(prefix)) remove.push(key);
      }
      for (const key of remove) storage.removeItem(key);
    },
  };
}

export function roomLocalStorage() { return scoped(window.localStorage); }
export function roomSessionStorage() { return scoped(window.sessionStorage); }
