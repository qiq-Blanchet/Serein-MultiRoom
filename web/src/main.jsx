import React from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/source-serif-4/400.css";
import "@fontsource/source-serif-4/400-italic.css";
import "@fontsource/noto-serif-sc/400.css";
import { initializeRoomScope } from "./storage/roomScopedStorage.js";
import "./styles.css";

const root = document.getElementById("root");
try {
  // Resolve the authenticated room BEFORE importing page/store modules.
  await initializeRoomScope();
  const { App } = await import("./App.jsx");
  createRoot(root).render(<React.StrictMode><App /></React.StrictMode>);
} catch {
  // Never load another room's stale browser state on a context error.
  root.textContent = "无法验证当前 AI 房间，请重新打开已授权的 Serein 页面。";
}
