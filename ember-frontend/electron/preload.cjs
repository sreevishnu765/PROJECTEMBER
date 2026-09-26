// Minimal, explicit bridge — the renderer gets exactly one thing it
// couldn't otherwise know: where the auto-launched backend is listening
// and what token to auth with. Nothing else is exposed; context isolation
// stays on and nodeIntegration stays off in the renderer.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("emberBackend", {
  getConnection: () => ipcRenderer.invoke("ember:get-connection"),
});

// Backend startup errors (missing module, bad token, etc.) only ever had
// a terminal to print to before — nothing for a double-clicked packaged
// app. Piping them into this window's own DevTools console (F12) means
// "why won't it connect" is answerable from the app itself.
ipcRenderer.on("ember:backend-log", (_event, line) => {
  console.log("[ember-backend]", line);
});
