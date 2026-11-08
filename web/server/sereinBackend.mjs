import { backendFor } from './roomContext.mjs';
// Server-only memory backend. Credentials never enter the browser bundle.
export function sereinConfigured(env = process.env) {
  return Boolean(backendFor(env).base);
}

export function sereinBackendTimedOut(error) {
  return error?.name === 'TimeoutError' || error?.name === 'AbortError';
}

export async function callSereinBackend(path, { method = "GET", body, timeoutMs, signal } = {}, { env = process.env, fetchImpl = fetch, timeout = 30_000 } = {}) {
  const {base: rawBase, token} = backendFor(env);
  const base = rawBase.replace(/\/$/, "");
  if (!base || !token) throw new Error("serein_backend_not_configured");
  const response = await fetchImpl(`${base}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs ?? timeout)]) : AbortSignal.timeout(timeoutMs ?? timeout),
  });
  return { ok: response.ok, status: response.status, payload: await response.json() };
}

export async function callSereinTool(name, args) {
  const result = await callSereinBackend("/v1/tools/call", { method: "POST", body: { name, arguments: args } });
  if (!result.ok) throw new Error(`serein_tool_${result.status}`);
  return result.payload.result;
}
