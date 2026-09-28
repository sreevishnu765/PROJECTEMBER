// Ember desktop shell.
//
// This does two jobs now, not one:
//  1. What it always did — a thin Electron window around the Vite/React
//     chat app.
//  2. NEW: launches the Python backend (run_transport.py) itself as a
//     child process, generates a local auth token once and reuses it, and
//     hands that token + the ws:// URL to the renderer over IPC — so
//     there's one thing to double-click, not "start the Python server in
//     one terminal, then run the frontend in another" every time.
//
// Scope, stated plainly: this assumes Python + the backend's own pip
// dependencies (google-genai, requests, etc.) are ALREADY installed on
// this machine — same as when you were running run_transport.py by hand.
// It does not bundle a Python interpreter or vendor those dependencies
// into the installer. Turning this into a true zero-prerequisite
// installer (PyInstaller-packaged backend, bundled Playwright browser,
// etc.) is materially more work and a separate task if you want it later.
const { app, BrowserWindow, Menu, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const crypto = require("node:crypto");

const isDev = !app.isPackaged;

Menu.setApplicationMenu(null);

// Mic capture now starts automatically on connect (wake-word listening), so
// there is no click to unlock audio playback — allow it without a gesture.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

// ---- Backend config -------------------------------------------------
// Overridable without touching code: create ember-frontend/ember.config.json
// with either/both fields to point at a real venv interpreter or a backend
// checked out somewhere else, e.g.:
//   { "pythonPath": "D:\\VISHNU\\PROJECTEMBER\\.venv\\Scripts\\python.exe" }
const DEFAULT_CONFIG = {
  pythonPath: "python",
  // ember-frontend/ sits directly inside the backend's project root
  // (run_transport.py, ember_core.py, etc. one level up) — matches the
  // actual PROJECTEMBER layout this was built against.
  backendDir: path.join(__dirname, "..", ".."),
  backendEntry: "run_transport.py",
};

function loadConfig() {
  // Packaged: ember.config.json ships as a plain, editable file next to
  // the app (not baked inside app.asar), so someone can fix pythonPath
  // after installing without unpacking anything. Dev: it's just the file
  // sitting at the project root.
  const configPath = app.isPackaged
    ? path.join(process.resourcesPath, "ember.config.json")
    : path.join(__dirname, "..", "ember.config.json");

  const defaults = app.isPackaged
    ? { ...DEFAULT_CONFIG, backendDir: null } // no sane relative guess once installed — must be set explicitly
    : DEFAULT_CONFIG;

  if (!fs.existsSync(configPath)) {
    if (app.isPackaged) {
      const message = `[ember] No config found at ${configPath}. Create it and set "backendDir" to wherever your Ember backend actually lives.`;
      console.error(message);
      sendBackendLog(message); // was console.error-only before — invisible in a packaged app with no attached terminal, exactly the failure mode this needed to surface
    }
    return defaults;
  }
  try {
    const overrides = JSON.parse(fs.readFileSync(configPath, "utf-8"));
    return { ...defaults, ...overrides };
  } catch (e) {
    const message = `[ember] couldn't parse ${configPath}, using defaults: ${e}`;
    console.error(message);
    sendBackendLog(message);
    return defaults;
  }
}

// One token per install, not per launch — regenerating it every run would
// make the previous run's connection info stale for no reason. Stored
// outside the project folder (Electron's userData dir) so it isn't
// accidentally committed if this project is under git.
function getOrCreateToken() {
  const tokenPath = path.join(app.getPath("userData"), "ember-token.txt");
  if (fs.existsSync(tokenPath)) {
    const existing = fs.readFileSync(tokenPath, "utf-8").trim();
    if (existing) return existing;
  }
  const token = crypto.randomBytes(24).toString("hex");
  fs.mkdirSync(path.dirname(tokenPath), { recursive: true });
  fs.writeFileSync(tokenPath, token, "utf-8");
  return token;
}

const WS_PORT = 8765;
const WS_URL = `ws://localhost:${WS_PORT}`;

let backendProcess = null;
let mainWindow = null;
// Real bug found here: the backend is launched before the window even
// exists (see app.whenReady() below), so any output it produces in that
// first instant — including a startup crash, which is exactly the kind
// of thing this logging exists to surface — had nowhere to go and was
// silently dropped, even though the forwarding code "worked." Buffered
// here and flushed once the renderer has actually loaded and attached
// its ipcRenderer.on listener (see win.webContents.on("did-finish-load")
// below) — reordering alone (create the window first) narrows this race
// but doesn't close it, since the page still takes a moment to load and
// run preload.cjs after the window object itself exists.
let backendLogBuffer = [];
let rendererReady = false;

function sendBackendLog(line) {
  backendLogBuffer.push(line);
  if (rendererReady && mainWindow) {
    while (backendLogBuffer.length) {
      mainWindow.webContents.send("ember:backend-log", backendLogBuffer.shift());
    }
  }
}

function startBackend(token) {
  const config = loadConfig();
  const entryPath = config.backendDir ? path.join(config.backendDir, config.backendEntry) : null;

  if (!entryPath || !fs.existsSync(entryPath)) {
    const message = `[ember] Backend entry not found${config.backendDir ? ` at ${entryPath}` : " (backendDir not set)"} — set "backendDir" in ember.config.json to your Ember project folder. The app window will still open, but nothing will be listening on ${WS_URL} until that's fixed.`;
    console.error(message);
    sendBackendLog(message); // same visibility fix as loadConfig()'s two branches above
    return;
  }

  console.log(`[ember] Launching backend: ${config.pythonPath} ${entryPath} (cwd: ${config.backendDir})`);
  backendProcess = spawn(config.pythonPath, [entryPath], {
    cwd: config.backendDir,
    env: {
      ...process.env,
      EMBER_TRANSPORT_TOKEN: token,
      // Real, reproduced crash on Windows: a piped stdout (this process
      // has no attached console) falls back to the system codepage
      // (often cp1252), which can't encode characters like "→" that show
      // up in some of ember_core.py's own log lines. That's an
      // unhandled UnicodeEncodeError, not a graceful failure — forcing
      // UTF-8 here fixes it at the source for any such character,
      // present or future, without needing to hunt down every print().
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
      // Real, reproduced bug: passing the script as a relative path
      // ("run_transport.py") and relying on `cwd` above to resolve it
      // did NOT reliably put the backend's own directory on sys.path the
      // same way running it directly with an absolute path does (which
      // is exactly how the person confirmed the backend works standalone)
      // — the result was "ModuleNotFoundError: No module named
      // 'ember_tools'" from a spawned child, never reproducible when run
      // by hand. Fixed two ways at once: entryPath (already absolute,
      // computed above) is now what's actually passed to Python instead
      // of the relative name, AND PYTHONPATH is set explicitly here as a
      // second, independent guarantee that backendDir's local packages
      // (ember_tools, etc.) are importable regardless of any other
      // sys.path quirk specific to how Windows/Node resolves a spawned
      // child's working directory.
      PYTHONPATH: config.backendDir,
    },
  });

  backendProcess.stdout.on("data", (data) => {
    process.stdout.write(`[ember-backend] ${data}`);
    sendBackendLog(data.toString());
  });
  backendProcess.stderr.on("data", (data) => {
    process.stderr.write(`[ember-backend] ${data}`);
    sendBackendLog(data.toString());
  });
  backendProcess.on("exit", (code) => {
    console.log(`[ember] Backend process exited (code ${code}).`);
    sendBackendLog(`[process exited with code ${code}]`);
    backendProcess = null;
  });
  backendProcess.on("error", (err) => {
    const message = `Failed to launch backend (is "${config.pythonPath}" on PATH?): ${err.message}`;
    console.error(`[ember] ${message}`);
    sendBackendLog(message);
  });
}

function stopBackend() {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 860,
    minHeight: 560,
    backgroundColor: "#0A0D12",
    titleBarStyle: "hiddenInset",
    icon: path.join(__dirname, "icon.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow = win;
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
  });

  // Flips once the page has actually loaded and preload.cjs has run —
  // only then is the renderer's ipcRenderer.on("ember:backend-log", ...)
  // listener guaranteed to be attached. Flushes anything the backend
  // already logged before this moment (very likely for a fast crash).
  win.webContents.on("did-finish-load", () => {
    rendererReady = true;
    while (backendLogBuffer.length) {
      win.webContents.send("ember:backend-log", backendLogBuffer.shift());
    }
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // F12 opens DevTools even in the packaged build — the only way to see a
  // blank-screen/renderer error without a terminal attached. Doesn't
  // auto-open on launch (that'd look broken/unfinished for a "premium"
  // app); it's there when something needs debugging.
  win.webContents.on("before-input-event", (_event, input) => {
    if (input.key === "F12" && input.type === "keyDown") {
      win.webContents.toggleDevTools();
    }
  });

  if (isDev) {
    win.loadURL("http://localhost:5173");
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

app.whenReady().then(() => {
  const token = getOrCreateToken();

  // Renderer asks for this once on load (see preload.cjs) — it doesn't
  // need to know the backend was auto-launched, just where to connect.
  ipcMain.handle("ember:get-connection", () => ({ url: WS_URL, token }));

  // Window created BEFORE the backend is spawned — not just for a
  // snappier launch, but so did-finish-load has the earliest possible
  // chance to fire and start receiving buffered backend logs. The
  // buffer above still covers the remaining gap (page load isn't
  // instant either), but there's no reason to make that gap any wider
  // than it has to be.
  createMainWindow();
  startBackend(token);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
});

app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", stopBackend);
