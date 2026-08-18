#!/usr/bin/env node

import { execFileSync, spawn } from "node:child_process";
import { mkdir, readFile, writeFile, chmod, unlink } from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright-core";

const host = "127.0.0.1";
const port = Number(process.env.LOCAL_BROWSER_CDP_PORT || "9222");
const endpoint = `http://${host}:${port}`;
const channel = process.env.LOCAL_BROWSER_CHANNEL || "msedge";
const profileDir = process.env.LOCAL_BROWSER_PROFILE_DIR ||
  path.join("/tmp", "edge-debug-profile");
const statePath = process.env.LOCAL_BROWSER_STATE_FILE ||
  path.join(os.homedir(), ".codex", "browser-profiles", "apple-developer.session.json");
const UTM10_EDGE_LAUNCH_SCRIPT = String.raw`pkill "Microsoft Edge"

nohup "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/edge-debug-profile \
  --no-first-run \
  --start-maximized \
  >/dev/null 2>&1 &
`;

function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function assertApprovedUrl(value) {
  const url = new URL(value);
  const approved = ["apple.com", "developer.apple.com", "appstoreconnect.apple.com", "idmsa.apple.com"];
  if (url.protocol !== "https:" || !approved.some((item) => url.hostname === item || url.hostname.endsWith(`.${item}`))) {
    throw new Error(`URL is outside the approved Apple domains: ${safeUrl(value)}`);
  }
  return url.toString();
}

async function versionInfo() {
  try {
    const response = await fetch(`${endpoint}/json/version`, { signal: AbortSignal.timeout(30000) });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

function sessionId(info) {
  return String(info.webSocketDebuggerUrl || "").split("/").pop();
}

async function readState() {
  try {
    return JSON.parse(await readFile(statePath, "utf8"));
  } catch {
    return null;
  }
}

async function writeState(info, pid = null) {
  await mkdir(path.dirname(statePath), { recursive: true });
  await writeFile(statePath, `${JSON.stringify({
    pid,
    channel,
    port,
    profileDir,
    sessionId: sessionId(info),
  }, null, 2)}\n`, { mode: 0o600 });
  await chmod(statePath, 0o600);
}

export async function currentSessionIdentity() {
  const [info, state] = await Promise.all([versionInfo(), readState()]);
  const liveSessionId = info ? sessionId(info) : null;
  if (!info || !state || !liveSessionId || state.sessionId !== liveSessionId) {
    throw new Error("Local browser session identity changed");
  }
  const pid = uniqueMainEdgePid();
  if (!pid || state.pid !== pid) throw new Error("Local browser PID changed");
  const edgeWebsocket = String(info.webSocketDebuggerUrl || "");
  if (!edgeWebsocket.startsWith("ws://127.0.0.1:9222/")) {
    throw new Error("Local browser WebSocket is invalid");
  }
  return {
    edge_pid: pid,
    edge_websocket: edgeWebsocket,
    sessionId: liveSessionId,
  };
}

async function runShell(script) {
  await new Promise((resolve, reject) => {
    const child = spawn("/bin/zsh", ["-lc", script], { stdio: "ignore" });
    child.once("error", reject);
    child.once("close", (code) => code === 0 ? resolve() : reject(new Error(`Edge launch commands exited ${code}`)));
  });
}

function uniqueMainEdgePid() {
  try {
    const values = execFileSync("/usr/bin/pgrep", ["-x", "Microsoft Edge"], { encoding: "utf8" })
      .split(/\s+/).filter(Boolean).map(Number).filter(Number.isInteger);
    return values.length === 1 && values[0] > 0 ? values[0] : null;
  } catch {
    return null;
  }
}

export async function restartLocalBrowser() {
  await mkdir(profileDir, { recursive: true });
  await runShell(UTM10_EDGE_LAUNCH_SCRIPT);
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const info = await versionInfo();
    const pid = uniqueMainEdgePid();
    if (info && pid) {
      await writeState(info, pid);
      return info;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Local browser did not expose its Playwright CDP endpoint");
}

async function ensureStarted() {
  const current = await versionInfo();
  const state = await readState();
  if (current) {
    if (state?.sessionId && state.sessionId !== sessionId(current)) {
      throw new Error(`CDP port ${port} belongs to a different browser session`);
    }
    if (!state) await writeState(current, null);
    return current;
  }
  throw new Error("UTM-10 browser is not running");
}

export async function connectLocalBrowser() {
  const info = await ensureStarted();
  const state = await readState();
  if (!state || state.sessionId !== sessionId(info)) {
    throw new Error("Local browser session identity changed");
  }
  const browser = await chromium.connectOverCDP(endpoint);
  const context = browser.contexts()[0];
  if (!context) throw new Error("Local browser has no persistent context");
  return { browser, context, sessionId: state.sessionId };
}

export async function approvedPage(url) {
  const approvedUrl = assertApprovedUrl(url);
  const { browser, context, sessionId: id } = await connectLocalBrowser();
  const existing = context.pages().find((page) => {
    try {
      return safeUrl(page.url()) === safeUrl(approvedUrl);
    } catch {
      return false;
    }
  });
  const reusable = context.pages().length === 1 ? context.pages()[0] : null;
  const page = existing || reusable || await context.newPage();
  if (!existing) await page.goto(approvedUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.bringToFront();
  return { browser, context, page, sessionId: id };
}

export async function exactlyOne(locator, label) {
  const count = await locator.count();
  if (count !== 1) throw new Error(`${label} must match exactly once; found ${count}`);
  const target = locator.first();
  if (!await target.isVisible()) throw new Error(`${label} is not visible`);
  return target;
}

export async function secureScreenshot(page, outputPath) {
  const target = path.resolve(outputPath);
  await mkdir(path.dirname(target), { recursive: true });
  const client = await page.context().newCDPSession(page);
  try {
    await client.send("Page.enable");
    const metrics = await client.send("Page.getLayoutMetrics");
    const size = metrics.cssContentSize || metrics.contentSize;
    const captured = await client.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
      captureBeyondViewport: true,
      clip: {
        x: 0,
        y: 0,
        width: Math.max(1, Math.ceil(size.width)),
        height: Math.max(1, Math.ceil(size.height)),
        scale: 1,
      },
    });
    await writeFile(target, Buffer.from(captured.data, "base64"), { mode: 0o600 });
  } finally {
    await client.detach().catch(() => {});
  }
  await chmod(target, 0o600);
  return target;
}

export async function readMembershipDetails() {
  const { context } = await connectLocalBrowser();
  let page = context.pages().find((candidate) => {
    try {
      const url = new URL(candidate.url());
      return url.hostname === "developer.apple.com" && url.pathname.startsWith("/account");
    } catch {
      return false;
    }
  });
  page ||= await context.newPage();
  await page.goto("https://developer.apple.com/account/", {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  await page.bringToFront();

  const deadline = Date.now() + 30_000;
  do {
    for (const frame of page.frames()) {
      const body = await frame.locator("body").innerText().catch(() => "");
      const team = body.match(/Team ID\s+([A-Z0-9]{10})\b/);
      const renewal = body.match(/Renewal date\s+([^\n\r]+)/i);
      if (team && renewal && renewal[1].trim()) {
        const info = await versionInfo();
        const state = await readState();
        return {
          team_id: team[1],
          renewal_date: renewal[1].trim(),
          edge_pid: state?.pid,
          edge_websocket: info?.webSocketDebuggerUrl,
        };
      }
    }
    await page.waitForTimeout(20);
  } while (Date.now() < deadline);
  throw new Error("Membership details were not readable");
}

async function status() {
  const info = await versionInfo();
  const state = await readState();
  process.stdout.write(`${JSON.stringify({
    verified: Boolean(info && state && state.sessionId === sessionId(info)),
    channel,
    endpoint,
    profileDir,
    sessionId: info ? sessionId(info) : null,
  })}\n`);
}

async function inspectPages() {
  const { context, sessionId: id } = await connectLocalBrowser();
  const pages = [];
  for (const page of context.pages()) {
    let approved = true;
    try { assertApprovedUrl(page.url()); } catch { approved = false; }
    if (!approved) continue;
    const bodyText = await page.locator("body").innerText().catch(() => "");
    const links = await page.locator("a").evaluateAll((items) => items.map((item) => ({
      text: String(item.innerText || item.textContent || "").replace(/\s+/g, " ").trim(),
      href: String(item.href || ""),
    })).filter((item) => item.text || item.href));
    pages.push({
      url: safeUrl(page.url()),
      title: await page.title(),
      body_text: bodyText.replace(/\s+/g, " ").trim().slice(0, 12000),
      links: links.slice(0, 500),
    });
  }
  process.stdout.write(
    `${JSON.stringify({ verified: true, sessionId: id, pages })}\n`,
    () => process.exit(0),
  );
}

async function stop() {
  const info = await versionInfo();
  const state = await readState();
  if (!info || !state || state.sessionId !== sessionId(info) || !state.pid) {
    throw new Error("Refusing to stop a browser not started by this helper");
  }
  process.kill(state.pid, "SIGTERM");
  await unlink(statePath).catch(() => {});
  process.stdout.write("LOCAL_BROWSER_STOPPED=verified\n");
}

async function main() {
  const [command, value] = process.argv.slice(2);
  if (command === "start") {
    await restartLocalBrowser();
    await status();
  } else if (command === "status") {
    await status();
  } else if (command === "inspect") {
    await inspectPages();
  } else if (command === "open") {
    if (!value) throw new Error("Usage: session.mjs open <https://approved-apple-url>");
    const { page, sessionId: id } = await approvedPage(value);
    process.stdout.write(`${JSON.stringify({ verified: true, sessionId: id, url: safeUrl(page.url()), title: await page.title() })}\n`, () => process.exit(0));
  } else if (command === "stop") {
    await stop();
  } else {
    throw new Error("Usage: session.mjs <start|status|inspect|open|stop> [value]");
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`LOCAL_BROWSER_ERROR=${error.message}\n`);
    process.exitCode = 1;
  });
}
