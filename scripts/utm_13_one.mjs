#!/usr/bin/env node

import { createHash, createPublicKey, randomUUID, X509Certificate } from "node:crypto";
import { spawn } from "node:child_process";
import {
  access,
  chmod,
  copyFile,
  mkdir,
  readFile,
  readdir,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const SESSION_MODULE_URL = new URL("./session.mjs", import.meta.url);
const PLAYWRIGHT_CORE_VERSION = "1.62.1";

async function installMissingPlaywrightCore() {
  const npmCandidates = [
    path.join(path.dirname(process.execPath), "npm"),
    "/opt/homebrew/bin/npm",
    "/usr/local/bin/npm",
    "/usr/bin/npm",
  ];
  let npm = null;
  for (const candidate of npmCandidates) {
    if (await access(candidate).then(() => true).catch(() => false)) {
      npm = candidate;
      break;
    }
  }
  if (!npm) throw new Error("playwright-core is missing and npm is unavailable");
  const target = path.dirname(fileURLToPath(SESSION_MODULE_URL));
  const installed = await new Promise((resolve, reject) => {
    const child = spawn(npm, [
      "install", "--prefix", target, "--no-save", "--no-package-lock", "--ignore-scripts",
      "--loglevel=error", `playwright-core@${PLAYWRIGHT_CORE_VERSION}`,
    ], { stdio: ["ignore", "ignore", "pipe"] });
    const stderr = [];
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => resolve({
      code,
      stderr: Buffer.concat(stderr).toString("utf8").trim(),
    }));
  });
  if (installed.code !== 0) {
    throw new Error(`playwright-core installation failed: ${installed.stderr || `exit ${installed.code}`}`);
  }
}

async function loadSessionModule() {
  try {
    return await import(SESSION_MODULE_URL.href);
  } catch (error) {
    if (error?.code !== "ERR_MODULE_NOT_FOUND" || !String(error.message).includes("playwright-core")) {
      throw error;
    }
  }
  await installMissingPlaywrightCore();
  return await import(`${SESSION_MODULE_URL.href}?dependency=${PLAYWRIGHT_CORE_VERSION}`);
}

const { connectLocalBrowser } = await loadSessionModule();

const TIMEOUT = 30_000;
const SHORT_TIMEOUT = 7_000;
const CERTIFICATES_URL = "https://developer.apple.com/account/resources/certificates/list";
const PROFILES_URL = "https://developer.apple.com/account/resources/profiles/list";
const PROFILE_ADD_PATH = "/account/resources/profiles/add";
const ACCOUNT_URL = "https://developer.apple.com/account/";
const PROJECT_ROOT = process.cwd();
const RUNTIME_ROOT = path.join(PROJECT_ROOT, "runtime", "utm-13");
const UTM12_ROOT = path.join(PROJECT_ROOT, "runtime", "utm-12");
const CSR_PATH = path.join(os.homedir(), "Desktop", "CertificateSigningRequest.certSigningRequest");
const VISIBLE_CERTIFICATE_PATH = path.join(os.homedir(), "Downloads", "distribution.cer");
const LOGIN_KEYCHAIN = path.join(os.homedir(), "Library", "Keychains", "login.keychain-db");
const SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain";
const PROFILE_DIR = process.env.LOCAL_BROWSER_PROFILE_DIR ||
  path.join("/tmp", "edge-debug-profile");
const STATE_PATH = process.env.LOCAL_BROWSER_STATE_FILE ||
  path.join(os.homedir(), ".codex", "browser-profiles", "apple-developer.session.json");
const CDP_PORT = Number(process.env.LOCAL_BROWSER_CDP_PORT || "9222");

function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function comparableUrl(value) {
  const url = new URL(value);
  const pathname = url.pathname.replace(/\/+$/, "") || "/";
  return `${url.protocol}//${url.host}${pathname}`;
}

function isApprovedApplePage(page) {
  try {
    const url = new URL(page.url());
    const approved = ["apple.com", "developer.apple.com", "appstoreconnect.apple.com", "idmsa.apple.com"];
    return url.protocol === "https:" && approved.some(
      (hostname) => url.hostname === hostname || url.hostname.endsWith(`.${hostname}`),
    );
  } catch {
    return false;
  }
}

function assertAppleUrl(value, expectedPathPrefix = null) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== "developer.apple.com") {
    throw new Error(`unexpected page: ${safeUrl(value)}`);
  }
  if (expectedPathPrefix && !url.pathname.startsWith(expectedPathPrefix)) {
    throw new Error(`unexpected Apple page: ${safeUrl(value)}`);
  }
  return url;
}

function hashBytes(value) {
  return createHash("sha256").update(value).digest("hex");
}

function hashJson(value) {
  return hashBytes(JSON.stringify(value));
}

function sessionDir(sessionId) {
  if (!/^[a-f0-9-]{20,}$/i.test(sessionId)) throw new Error("invalid browser session ID");
  return path.join(RUNTIME_ROOT, sessionId);
}

function runPath(sessionId) {
  return path.join(sessionDir(sessionId), "run.json");
}

function certificateLedgerPath(sessionId) {
  return path.join(sessionDir(sessionId), "certificate-attempt.json");
}

function profileLedgerPath(sessionId) {
  return path.join(sessionDir(sessionId), "profile-attempt.json");
}

function archivedProfileLedgerPath(sessionId, attemptId) {
  if (!/^[a-z0-9-]{20,}$/i.test(attemptId)) throw new Error("invalid archived profile attempt ID");
  return path.join(sessionDir(sessionId), "profile-attempts", `${attemptId}.json`);
}

function profileCorrectionPath(sessionId) {
  return path.join(sessionDir(sessionId), "profile-name-correction.json");
}

async function readJson(file) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function writeSecureJson(file, value) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  await chmod(path.dirname(file), 0o700);
  const temporary = `${file}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  await chmod(temporary, 0o600);
  await rename(temporary, file);
  await chmod(file, 0o600);
}

async function command(program, args, { input = null } = {}) {
  return await new Promise((resolve, reject) => {
    const child = spawn(program, args, { stdio: ["pipe", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => resolve({
      code,
      stdout: Buffer.concat(stdout).toString("utf8"),
      stderr: Buffer.concat(stderr).toString("utf8"),
    }));
    if (input !== null) child.stdin.end(input);
    else child.stdin.end();
  });
}

async function verifyRequiredCommands() {
  const checks = [
    ["/usr/bin/openssl", ["version"]],
    ["/usr/bin/security", ["help"]],
    ["/usr/bin/sudo", ["-V"]],
  ];
  for (const [program, args] of checks) {
    await access(program);
    const result = await command(program, args);
    if (result.code !== 0) throw new Error(`required command is unavailable: ${program}`);
  }
}

async function readStdinAuthorization() {
  let raw;
  if (process.stdin.isTTY) {
    const canSetRawMode = typeof process.stdin.setRawMode === "function";
    const previousRawMode = Boolean(process.stdin.isRaw);
    if (canSetRawMode) process.stdin.setRawMode(true);
    try {
      const input = createInterface({ input: process.stdin, terminal: false });
      raw = await new Promise((resolve) => {
        let settled = false;
        input.once("line", (line) => {
          settled = true;
          input.close();
          resolve(line.trim());
        });
        input.once("close", () => {
          if (!settled) resolve("");
        });
      });
    } finally {
      if (canSetRawMode) process.stdin.setRawMode(previousRawMode);
    }
  } else {
    const chunks = [];
    let length = 0;
    for await (const chunk of process.stdin) {
      length += chunk.length;
      if (length > 65_536) throw new Error("stdin authorization is too large");
      chunks.push(chunk);
    }
    raw = Buffer.concat(chunks).toString("utf8").trim();
  }
  if (!raw) return {};
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("stdin authorization must be a JSON object");
  }
  const allowed = new Set(["authorizationAttemptId", "systemKeychainPassword", "certificateUserName"]);
  for (const key of Object.keys(parsed)) if (!allowed.has(key)) throw new Error(`unsupported stdin field: ${key}`);
  return parsed;
}

async function browserVersion() {
  try {
    const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, {
      signal: AbortSignal.timeout(SHORT_TIMEOUT),
    });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

function sessionIdFromVersion(info) {
  return String(info?.webSocketDebuggerUrl || "").split("/").pop();
}

async function connectExistingSession() {
  const [info, state] = await Promise.all([browserVersion(), readJson(STATE_PATH)]);
  const liveSessionId = sessionIdFromVersion(info);
  if (!info || !state || !liveSessionId) {
    throw new Error("utm-12 browser is not running; refusing to start another browser");
  }
  if (state.sessionId !== liveSessionId) throw new Error("local browser session changed");
  const connected = await connectLocalBrowser();
  if (connected.sessionId !== liveSessionId) throw new Error("connected session does not match utm-12");
  return connected;
}

function firstString(object, paths) {
  for (const keys of paths) {
    let value = object;
    for (const key of keys) value = value?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

async function readUtm12Handoff(sessionId) {
  const candidates = [
    path.join(UTM12_ROOT, sessionId, "run.json"),
    path.join(UTM12_ROOT, "run.json"),
  ];
  let source = null;
  for (const candidate of candidates) {
    const value = await readJson(candidate);
    if (value?.sessionId === sessionId) {
      source = value;
      break;
    }
  }
  if (!source) throw new Error("utm-12 verified run for this browser session is missing");
  const verified = source.state === "verified" || source.UTM_12 === "verified" || source.markers?.UTM_12 === "verified";
  if (!verified) throw new Error("utm-12 run is not verified");

  for (const checkpoint of ["developerAccount", "appId", "appStoreAgreement", "appStoreApp"]) {
    if (source.checkpoints?.[checkpoint]?.state !== "verified") {
      throw new Error(`utm-12 checkpoint is not verified: ${checkpoint}`);
    }
  }
  const [registerLedger, appLedger] = await Promise.all([
    readJson(path.join(UTM12_ROOT, sessionId, "app-id-register-attempt.json")),
    readJson(path.join(UTM12_ROOT, sessionId, "app-create-attempt.json")),
  ]);
  if (registerLedger?.state !== "submitted" || appLedger?.state !== "submitted") {
    throw new Error("utm-12 irreversible-action ledgers are incomplete");
  }

  const inputCandidates = [
    path.join(PROJECT_ROOT, "config", "utm-12.json"),
    path.join(PROJECT_ROOT, "utm-12.json"),
    path.join(UTM12_ROOT, "input.json"),
  ];
  const inputs = [];
  for (const candidate of inputCandidates) {
    const value = await readJson(candidate);
    if (value) inputs.push({ candidate, value });
  }
  if (!inputs.length) throw new Error("utm-12 application input is missing");
  if (new Set(inputs.map(({ value }) => hashJson(value))).size !== 1) {
    throw new Error("utm-12 application inputs conflict");
  }
  for (const { candidate } of inputs) {
    if (((await stat(candidate)).mode & 0o777) !== 0o600) {
      throw new Error(`utm-12 application input mode must be 600: ${path.basename(candidate)}`);
    }
  }
  const input = inputs[0].value;

  const teamId = firstString(source, [
    ["handoff", "teamId"], ["membership", "teamId"], ["teamId"], ["TEAM_ID"],
  ]);
  const bundleId = firstString(source, [
    ["handoff", "bundleId"], ["app", "bundleId"], ["bundleId"], ["BUNDLE_ID"],
  ]) || firstString(input, [["bundleId"], ["BUNDLE_ID"]]);
  const appName = firstString(source, [
    ["handoff", "appName"], ["app", "name"], ["appName"], ["APP_NAME"],
  ]) || firstString(input, [["appName"], ["APP_NAME"]]);
  const providedProfileName = firstString(source, [
    ["handoff", "profileName"], ["profileName"],
  ]);
  if (providedProfileName && appName && providedProfileName !== appName) {
    throw new Error("utm-12 profileName must exactly equal appName");
  }
  const profileName = appName;

  if (teamId && !/^[A-Z0-9]{10}$/.test(teamId)) throw new Error("utm-12 Team ID is invalid");
  if (!/^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/.test(bundleId || "")) {
    throw new Error("utm-12 Bundle ID is missing or invalid");
  }
  if (!appName) throw new Error("utm-12 app name is missing");
  if (profileName.length > 100) throw new Error("derived provisioning profile name is too long");
  const membershipHash = source.checkpoints?.developerAccount?.membershipHash;
  if (!teamId && !/^[a-f0-9]{64}$/i.test(membershipHash || "")) {
    throw new Error("utm-12 membership evidence is missing");
  }
  return { teamId, bundleId, appName, profileName, membershipHash };
}

async function validateCsrOnce() {
  const metadata = await stat(CSR_PATH);
  if (!metadata.isFile() || metadata.size < 256 || metadata.size > 64 * 1024) {
    throw new Error("CSR must be a regular file with a plausible size");
  }
  const [contents, verify, publicKey] = await Promise.all([
    readFile(CSR_PATH),
    command("/usr/bin/openssl", ["req", "-in", CSR_PATH, "-noout", "-verify"]),
    command("/usr/bin/openssl", ["req", "-in", CSR_PATH, "-noout", "-pubkey"]),
  ]);
  if (verify.code !== 0 || publicKey.code !== 0 || !publicKey.stdout.includes("BEGIN PUBLIC KEY")) {
    throw new Error("CSR format or self-signature verification failed");
  }
  let key;
  let spki;
  try {
    key = createPublicKey(publicKey.stdout);
    spki = key.export({ type: "spki", format: "der" });
  } catch {
    throw new Error("CSR public key cannot be parsed");
  }
  return {
    size: metadata.size,
    sha256: hashBytes(contents),
    publicKeySha256: hashBytes(spki),
  };
}

async function validateStableCsr() {
  const first = await validateCsrOnce();
  const second = await validateCsrOnce();
  if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error("CSR changed between validations");
  return first;
}

function parseIdentities(output, teamId) {
  const identities = [];
  for (const line of output.split(/\r?\n/)) {
    const match = line.match(/^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"]+)"/);
    if (!match) continue;
    const [, sha1, commonName] = match;
    if (!commonName.startsWith("Apple Distribution:")) continue;
    if (!commonName.includes(`(${teamId})`)) continue;
    identities.push({ sha1: sha1.toUpperCase(), commonName });
  }
  identities.sort((a, b) => a.sha1.localeCompare(b.sha1));
  return identities;
}

async function identityInventory(teamId) {
  const result = await command("/usr/bin/security", ["find-identity", "-v", "-p", "codesigning"]);
  if (result.code !== 0) throw new Error("unable to inventory codesigning identities");
  return parseIdentities(result.stdout, teamId);
}

async function stableIdentityInventory(teamId) {
  const first = await identityInventory(teamId);
  const second = await identityInventory(teamId);
  if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error("codesigning identity inventory changed");
  if (second.length > 1) throw new Error("multiple valid Apple Distribution identities match this Team");
  return second;
}

async function visible(locator) {
  const items = [];
  for (let index = 0; index < await locator.count(); index += 1) {
    const item = locator.nth(index);
    if (await item.isVisible().catch(() => false)) items.push(item);
  }
  return items;
}

async function visibleInFrames(page, factory) {
  const items = [];
  for (const frame of page.frames()) {
    try {
      items.push(...await visible(factory(frame)));
    } catch {
      // A frame may navigate while its state is being read.
    }
  }
  return items;
}

async function exactlyOne(page, factory, label, { enabled = false } = {}) {
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    const items = await visibleInFrames(page, factory);
    if (items.length > 1) throw new Error(`${label} must be uniquely visible; found ${items.length}`);
    if (items.length === 1 && (!enabled || await items[0].isEnabled().catch(() => false))) return items[0];
    await page.waitForTimeout(100);
  }
  throw new Error(`${label} did not become uniquely visible and enabled`);
}

async function uniqueRoleAction(page, name, label = name) {
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    const items = [
      ...await visibleInFrames(page, (frame) => frame.getByRole("button", { name, exact: true })),
      ...await visibleInFrames(page, (frame) => frame.getByRole("link", { name, exact: true })),
    ];
    if (items.length > 1) throw new Error(`${label} must be uniquely visible; found ${items.length}`);
    if (items.length === 1 && await items[0].isEnabled().catch(() => true)) return items[0];
    await page.waitForTimeout(100);
  }
  throw new Error(`${label} did not become uniquely visible and enabled`);
}

async function waitForSafePage(page, expectedPathPrefix = null) {
  await page.locator("body").waitFor({ state: "visible", timeout: TIMEOUT });
  assertAppleUrl(page.url(), expectedPathPrefix);
  const challenge = await visibleInFrames(page, (frame) => frame.getByText(
    /captcha|verify you are human|unusual activity|security challenge/i,
  ));
  if (challenge.length) throw new Error("Apple security challenge detected");
  const login = await visibleInFrames(page, (frame) => frame.locator(
    "input#account_name_text_field, input[autocomplete~='username'], input[type='password']",
  ));
  if (login.length) throw new Error("Apple login is required; rerun utm-10 first");
}

async function openPage(page, url, expectedPathPrefix) {
  if (!page || page.isClosed() || !isApprovedApplePage(page)) {
    throw new Error("locked Apple workflow page is unavailable; refusing to create a new page");
  }
  if (comparableUrl(page.url()) !== comparableUrl(url)) {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  await page.bringToFront();
  await waitForSafePage(page, expectedPathPrefix);
  return page;
}

async function membershipValue(section, label) {
  const labels = await visible(section.getByText(label, { exact: true }));
  if (labels.length !== 1) throw new Error(`${label} must be uniquely visible in Membership details`);
  const value = await labels[0].evaluate((element) => {
    const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
    const wanted = normalize(element.textContent);
    const row = element.closest("tr, li, [role='row'], dl > div, .row") || element.parentElement;
    if (!row) return "";
    const chunks = [...row.querySelectorAll("dd, td, [role='cell'], span, p, div")]
      .map((node) => normalize(node.textContent))
      .filter((text) => text && text !== wanted && !text.startsWith(`${wanted} `));
    if (chunks.length) return chunks.sort((a, b) => a.length - b.length)[0];
    return normalize(row.textContent).replace(wanted, "").trim();
  });
  if (!value) throw new Error(`${label} is empty`);
  return value;
}

async function readVerifiedAccountTeam(workflowPage, handoff) {
  const page = await openPage(workflowPage, ACCOUNT_URL, "/account");
  const deadline = Date.now() + TIMEOUT;
  let markers = [];
  while (Date.now() < deadline) {
    assertAppleUrl(page.url(), "/account");
    markers = await visibleInFrames(
      page,
      (frame) => frame.getByRole("heading", { name: "Membership details", exact: true }),
    );
    if (markers.length === 0) {
      markers = await visibleInFrames(
        page,
        (frame) => frame.locator("main").getByText("Membership details", { exact: true }),
      );
    }
    if (markers.length > 1) throw new Error("Membership details heading has multiple visible matches");
    if (markers.length === 1) break;
    await page.waitForTimeout(250);
  }
  if (markers.length !== 1) {
    throw new Error(`Membership details did not become visible at ${safeUrl(page.url())}`);
  }
  const section = markers[0].locator(
    "xpath=ancestor::*[.//*[normalize-space()='Team ID'] and .//*[normalize-space()='Renewal Date' or normalize-space()='Renewal date']][1]",
  );
  while (Date.now() < deadline) {
    const count = await section.count();
    if (count > 1) throw new Error("Membership details container has multiple matches");
    if (count === 1 && await section.isVisible()) break;
    await page.waitForTimeout(100);
  }
  if (await section.count() !== 1 || !await section.isVisible()) {
    throw new Error("Membership details container did not become uniquely visible");
  }
  const teamId = await membershipValue(section, "Team ID");
  const renewalDate = await membershipValue(section, "Renewal Date").catch(async () =>
    await membershipValue(section, "Renewal date"));
  if (!/^[A-Z0-9]{10}$/.test(teamId)) throw new Error("live Team ID is invalid");
  if (handoff.teamId && handoff.teamId !== teamId) throw new Error("live Team ID conflicts with utm-12 handoff");
  if (handoff.membershipHash && hashJson({ teamId, renewalDate }) !== handoff.membershipHash) {
    throw new Error("live Membership details do not match utm-12 evidence");
  }
  return teamId;
}

async function readTeamFromCertificatesPage(workflowPage, handoff) {
  const page = await openPage(workflowPage, CERTIFICATES_URL, "/account/resources/certificates");
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
    const teamIds = [...new Set(
      [...body.matchAll(/\s-\s([A-Z0-9]{10})\b/g)].map((match) => match[1]),
    )];
    if (teamIds.length > 1) throw new Error("Certificates page contains multiple Team IDs");
    if (teamIds.length === 1) {
      if (handoff.teamId && handoff.teamId !== teamIds[0]) {
        throw new Error("Certificates page Team ID conflicts with utm-12 handoff");
      }
      return teamIds[0];
    }
    await page.waitForTimeout(250);
  }

  const teamId = await readVerifiedAccountTeam(workflowPage, handoff);
  await openPage(workflowPage, CERTIFICATES_URL, "/account/resources/certificates");
  return teamId;
}

function dateLooksCurrent(text) {
  if (/\b(active|valid)\b/i.test(text)) return true;
  const patterns = text.match(/(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}/gi) || [];
  return patterns.some((value) => {
    const timestamp = Date.parse(value);
    return Number.isFinite(timestamp) && timestamp > Date.now();
  });
}

async function currentTableRows(page) {
  const strategies = [
    (frame) => frame.locator("main table tbody tr"),
    (frame) => frame.locator("main [role='row']"),
    (frame) => frame.locator("main li"),
  ];
  for (const strategy of strategies) {
    const rows = [];
    for (const row of await visibleInFrames(page, strategy)) {
      const text = (await row.innerText()).replace(/\s+/g, " ").trim();
      if (!text) continue;
      let cells = (await row.locator("th, td, [role='cell'], [role='gridcell']").allInnerTexts())
        .map((value) => value.replace(/\s+/g, " ").trim())
        .filter(Boolean);
      if (!cells.length) cells = [text];
      rows.push({ row, text, cells });
    }
    if (rows.length) return rows;
  }
  return [];
}

async function stableTableRows(page, label) {
  await page.waitForLoadState("networkidle", { timeout: TIMEOUT }).catch(() => {});
  const deadline = Date.now() + TIMEOUT;
  let previousSignature = null;
  let stableSamples = 0;
  while (Date.now() < deadline) {
    const loading = await visibleInFrames(page, (frame) => frame.getByText(/^Loading(?:…|\.\.\.)?$/i));
    const main = await visibleInFrames(page, (frame) => frame.locator("main"));
    const rows = await currentTableRows(page);
    const signature = hashJson(rows.map(({ cells }) => cells));
    const rowsRequired = label === "Distribution certificate selection";
    const ready = main.length === 1 && (!rowsRequired || rows.length > 0);
    if (!loading.length && ready && signature === previousSignature) stableSamples += 1;
    else stableSamples = 0;
    if (stableSamples >= 2) return rows;
    previousSignature = signature;
    await page.waitForTimeout(250);
  }
  throw new Error(`${label} did not reach a stable table state`);
}

function normalizedUserName(value) {
  return String(value || "").replace(/\s+/g, " ").trim().toLocaleUpperCase("en-US");
}

async function certificateCandidates(page, expectedUserName) {
  const rows = await stableTableRows(page, "certificate list");
  const typed = rows.filter(({ text }) => /\b(?:Apple )?Distribution\b/i.test(text));
  const expected = normalizedUserName(expectedUserName);
  if (!expected) throw new Error("certificate user name is missing");
  const exactName = typed.filter(({ cells, text }) => {
    const values = cells.length ? cells : [text];
    return values.some((value) => normalizedUserName(value) === expected);
  });
  return exactName;
}

async function clickRow(row, label) {
  const links = await visible(row.getByRole("link"));
  if (links.length === 1) await links[0].click({ timeout: SHORT_TIMEOUT });
  else if (links.length === 0) await row.click({ timeout: SHORT_TIMEOUT });
  else throw new Error(`${label} contains multiple visible links`);
}

async function clickAdd(page, label) {
  const pathname = new URL(page.url()).pathname;
  const addSuffix = pathname.includes("/certificates")
    ? "/account/resources/certificates/add"
    : pathname.includes("/profiles")
      ? "/account/resources/profiles/add"
      : null;
  const strategies = [
    (frame) => addSuffix ? frame.locator(`main a[href$="${addSuffix}"]`) : frame.locator("main a[href$='/add']"),
    (frame) => frame.locator("main").getByRole("link", { name: /^(Add|Create|\+)$/i }),
    (frame) => frame.locator("main").getByRole("button", { name: /^(Add|Create|\+)$/i }),
  ];
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    for (const strategy of strategies) {
      const candidates = await visibleInFrames(page, strategy);
      if (candidates.length > 1) {
        const hrefs = await Promise.all(candidates.map(async (candidate) => {
          const href = await candidate.getAttribute("href");
          return href ? new URL(href, page.url()).toString() : null;
        }));
        const uniqueHrefs = new Set(hrefs.filter(Boolean));
        if (addSuffix && uniqueHrefs.size === 1) {
          const target = new URL([...uniqueHrefs][0]);
          if (target.hostname === "developer.apple.com" && target.pathname === addSuffix) {
            await page.goto(target.toString(), { waitUntil: "domcontentloaded", timeout: TIMEOUT });
            return;
          }
        }
        throw new Error(`${label} must be unique; found ${candidates.length}`);
      }
      if (candidates.length === 1) {
        await candidates[0].click({ timeout: SHORT_TIMEOUT });
        return;
      }
    }
    await page.waitForTimeout(100);
  }
  throw new Error(`${label} is not visible`);
}

async function chooseExactRadio(page, exactText, label) {
  const text = await exactlyOne(page, (frame) => frame.getByText(exactText, { exact: true }), label);
  const radio = text.locator("xpath=ancestor-or-self::label[1]//input[@type='radio']");
  if (await radio.count() === 1) {
    await radio.check({ timeout: SHORT_TIMEOUT });
    if (!await radio.isChecked()) throw new Error(`${label} selection did not persist`);
    return;
  }
  const labelled = await visibleInFrames(page, (frame) => frame.getByRole("radio", { name: exactText, exact: true }));
  if (labelled.length === 1) {
    await labelled[0].check({ timeout: SHORT_TIMEOUT });
    if (!await labelled[0].isChecked()) throw new Error(`${label} selection did not persist`);
    return;
  }
  if (labelled.length > 1) throw new Error(`${label} radio must be unique; found ${labelled.length}`);

  await text.click({ timeout: SHORT_TIMEOUT });
  const checked = await visibleInFrames(page, (frame) => frame.locator("main input:checked"));
  if (checked.length > 1) throw new Error(`${label} produced multiple checked controls`);
  await uniqueRoleAction(page, "Continue", `${label} selected-state Continue`);
}

async function prepareLedger(file, details) {
  const existing = await readJson(file);
  const inputSummaryHash = hashJson(details);
  if (existing) {
    if (existing.sessionId !== details.sessionId || existing.inputSummaryHash !== inputSummaryHash) {
      throw new Error(`existing ${details.kind} ledger does not match this session or inputs`);
    }
    return existing;
  }
  const ledger = {
    attemptId: `${details.kind}-${hashJson(details).slice(0, 24)}`,
    kind: details.kind,
    sessionId: details.sessionId,
    pageIdentity: details.pageIdentity,
    inputSummaryHash,
    state: "planned",
    createdAt: new Date().toISOString(),
  };
  await writeSecureJson(file, ledger);
  return ledger;
}

async function updateLedger(file, ledger, state) {
  const updated = { ...ledger, state, updatedAt: new Date().toISOString() };
  await writeSecureJson(file, updated);
  return updated;
}

async function createCertificate(page, sessionId, handoff, csr) {
  const ledgerFile = certificateLedgerPath(sessionId);
  let ledger = await readJson(ledgerFile);
  if (ledger && ledger.state !== "planned") {
    throw new Error(`certificate attempt is ${ledger.state}; only read-only recovery is allowed`);
  }

  await clickAdd(page, "add certificate action");
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "/account/resources/certificates");
  await chooseExactRadio(page, "Apple Distribution", "Apple Distribution certificate type");
  await (await uniqueRoleAction(page, "Continue")).click({ timeout: SHORT_TIMEOUT });

  const upload = await exactlyOne(
    page,
    (frame) => frame.locator('input[type="file"]'),
    "CSR file input",
  );
  await upload.setInputFiles(CSR_PATH);
  const selected = path.basename((await upload.inputValue()).replace(/^C:\\fakepath\\/i, ""));
  if (selected !== path.basename(CSR_PATH)) throw new Error("CSR selected filename did not persist");

  const create = await uniqueRoleAction(page, "Continue", "certificate creation Continue");
  const pageIdentity = { url: safeUrl(page.url()), titleHash: hashBytes(await page.title()) };
  ledger = await prepareLedger(ledgerFile, {
    kind: "apple-distribution-certificate",
    sessionId,
    pageIdentity,
    teamHash: hashBytes(handoff.teamId),
    csrSha256: csr.sha256,
    csrPublicKeySha256: csr.publicKeySha256,
  });
  if (ledger.state !== "planned") throw new Error(`certificate attempt is ${ledger.state}; refusing to click again`);

  const liveSessionId = sessionIdFromVersion(await browserVersion());
  if (liveSessionId !== sessionId) throw new Error("browser session changed before certificate creation");
  assertAppleUrl(page.url(), "/account/resources/certificates");
  ledger = await updateLedger(ledgerFile, ledger, "clicking");
  try {
    await create.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(ledgerFile, ledger, "unknown");
    throw new Error(`certificate creation result is unknown: ${error.message}`);
  }
  try {
    await page.getByText("Download", { exact: true }).first().waitFor({ state: "visible", timeout: TIMEOUT });
  } catch {
    await updateLedger(ledgerFile, ledger, "unknown");
    throw new Error("certificate creation was clicked once, but Download did not become available");
  }
  return await updateLedger(ledgerFile, ledger, "submitted");
}

async function validateCer(file) {
  const metadata = await stat(file);
  if (!metadata.isFile() || metadata.size < 256 || metadata.size > 1024 * 1024) {
    throw new Error("downloaded certificate has an invalid size");
  }
  let result = await command("/usr/bin/openssl", ["x509", "-inform", "DER", "-in", file, "-noout", "-subject", "-dates", "-fingerprint"]);
  let format = "DER";
  if (result.code !== 0) {
    result = await command("/usr/bin/openssl", ["x509", "-inform", "PEM", "-in", file, "-noout", "-subject", "-dates", "-fingerprint"]);
    format = "PEM";
  }
  if (result.code !== 0 || !/Apple Distribution/i.test(result.stdout)) {
    throw new Error("download is not an Apple Distribution certificate");
  }
  const contents = await readFile(file);
  let certificate;
  try {
    certificate = new X509Certificate(contents);
  } catch {
    throw new Error("downloaded certificate cannot be parsed as X.509");
  }
  const now = Date.now();
  if (Date.parse(certificate.validFrom) > now || Date.parse(certificate.validTo) <= now) {
    throw new Error("downloaded Apple Distribution certificate is not currently valid");
  }
  return {
    size: metadata.size,
    sha256: hashBytes(contents),
    fingerprintSha1: certificate.fingerprint.replaceAll(":", "").toUpperCase(),
    fingerprint256: certificate.fingerprint256.replaceAll(":", "").toUpperCase(),
    publicKeySha256: hashBytes(certificate.publicKey.export({ type: "spki", format: "der" })),
    commonName: certificate.subject.match(/(?:^|\n)CN=([^\n]+)/)?.[1]?.trim() || null,
    validTo: certificate.validTo,
    format,
  };
}

async function exposeDistributionCertificate(file, evidence) {
  const existing = await stat(VISIBLE_CERTIFICATE_PATH).catch((error) => {
    if (error?.code === "ENOENT") return null;
    throw error;
  });
  if (existing) {
    const existingEvidence = await validateCer(VISIBLE_CERTIFICATE_PATH);
    if (existingEvidence.sha256 !== evidence.sha256) {
      throw new Error("Downloads/distribution.cer already exists with different certificate content");
    }
    await chmod(VISIBLE_CERTIFICATE_PATH, 0o600);
    return VISIBLE_CERTIFICATE_PATH;
  }
  await mkdir(path.dirname(VISIBLE_CERTIFICATE_PATH), { recursive: true, mode: 0o700 });
  await copyFile(file, VISIBLE_CERTIFICATE_PATH);
  await chmod(VISIBLE_CERTIFICATE_PATH, 0o600);
  const copied = await validateCer(VISIBLE_CERTIFICATE_PATH);
  if (copied.sha256 !== evidence.sha256) throw new Error("visible distribution.cer copy verification failed");
  return VISIBLE_CERTIFICATE_PATH;
}

async function recoverCertificateDownload(sessionId, attemptId, csr) {
  const downloadDir = path.join(sessionDir(sessionId), "downloads");
  await mkdir(downloadDir, { recursive: true, mode: 0o700 });
  await chmod(downloadDir, 0o700);
  const directoryEntries = await readdir(downloadDir);
  const existing = directoryEntries.filter((name) =>
    name.startsWith(`${attemptId}-`) && name.endsWith(".cer"));
  const matchingDownloads = [];
  for (const name of existing) {
    const file = path.join(downloadDir, name);
    const evidence = await validateCer(file).catch(() => null);
    if (evidence?.publicKeySha256 === csr.publicKeySha256) matchingDownloads.push({ file, evidence });
  }
  const uniqueMatchingHashes = new Set(matchingDownloads.map(({ evidence }) => evidence.sha256));
  if (uniqueMatchingHashes.size > 1) {
    throw new Error("multiple different CSR-matching certificate downloads exist for this attempt");
  }
  if (matchingDownloads.length) {
    const selected = matchingDownloads.sort((a, b) => a.file.localeCompare(b.file))[0];
    await exposeDistributionCertificate(selected.file, selected.evidence);
    return { ...selected, visibleFile: VISIBLE_CERTIFICATE_PATH };
  }
  return null;
}

async function downloadCertificate(page, sessionId, attemptId, csr) {
  const recovered = await recoverCertificateDownload(sessionId, attemptId, csr);
  if (recovered) return recovered;

  const downloadDir = path.join(sessionDir(sessionId), "downloads");
  const before = new Set(await readdir(downloadDir));

  const action = await uniqueRoleAction(page, "Download", "certificate Download");
  const downloadPromise = page.waitForEvent("download", { timeout: TIMEOUT });
  await action.click({ timeout: SHORT_TIMEOUT });
  const download = await downloadPromise;
  const suggested = path.basename(download.suggestedFilename());
  if (!/\.cer$/i.test(suggested)) throw new Error("certificate download does not have a .cer filename");
  const staging = path.join(downloadDir, `${attemptId}.${process.pid}.download`);
  if (await stat(staging).catch((error) => error?.code === "ENOENT" ? null : Promise.reject(error))) {
    throw new Error("certificate download staging file already exists");
  }
  await download.saveAs(staging);
  await chmod(staging, 0o600);
  const evidence = await validateCer(staging);
  if (evidence.publicKeySha256 !== csr.publicKeySha256) {
    throw new Error("downloaded certificate does not match the Desktop CSR public key");
  }
  const file = path.join(downloadDir, `${attemptId}-${evidence.sha256.slice(0, 24)}.cer`);
  if (await stat(file).catch((error) => error?.code === "ENOENT" ? null : Promise.reject(error))) {
    throw new Error("validated certificate evidence path already exists unexpectedly");
  }
  await rename(staging, file);
  await chmod(file, 0o600);
  const after = new Set(await readdir(downloadDir));
  const added = [...after].filter((name) => !before.has(name));
  if (added.length !== 1 || path.join(downloadDir, added[0]) !== file) {
    throw new Error("certificate download directory changed unexpectedly");
  }
  const savedEvidence = await validateCer(file);
  if (savedEvidence.sha256 !== evidence.sha256) throw new Error("saved certificate hash changed");
  await exposeDistributionCertificate(file, savedEvidence);
  return { file, evidence: savedEvidence, visibleFile: VISIBLE_CERTIFICATE_PATH };
}

async function importIntoSystemKeychain(file, authorization, attemptId) {
  const importArgs = [
    "/usr/bin/security", "import", file, "-k", SYSTEM_KEYCHAIN, "-T", "/usr/bin/codesign",
  ];
  let imported = await command("/usr/bin/sudo", ["-n", ...importArgs]);
  if (imported.code === 0) return imported;

  const passwordValue = authorization.systemKeychainPassword;
  if (!passwordValue) {
    throw new Error(`SYSTEM_KEYCHAIN_AUTHORIZATION_REQUIRED authorizationAttemptId=${attemptId}`);
  }
  if (authorization.authorizationAttemptId === "current-run") {
    authorization.authorizationAttemptId = attemptId;
  }
  if (authorization.authorizationAttemptId !== attemptId) {
    throw new Error("stdin System keychain authorization does not belong to this certificate attempt");
  }
  let password = String(passwordValue);
  imported = await command("/usr/bin/sudo", ["-S", "-p", "", ...importArgs], {
    input: `${password}\n`,
  });
  password = null;
  authorization.systemKeychainPassword = null;
  return imported;
}

async function certificateFingerprint256(file) {
  try {
    return new X509Certificate(await readFile(file)).fingerprint256.replaceAll(":", "").toUpperCase();
  } catch {
    throw new Error("certificate fingerprint cannot be read");
  }
}

async function keychainCertificateFingerprints(keychain, label) {
  const found = await command("/usr/bin/security", [
    "find-certificate", "-a", "-p", keychain,
  ]);
  if (found.code !== 0 && !found.stdout.trim()) return new Set();
  const blocks = found.stdout.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g) || [];
  const fingerprints = new Set();
  for (const block of blocks) {
    try {
      fingerprints.add(new X509Certificate(block).fingerprint256.replaceAll(":", "").toUpperCase());
    } catch {
      throw new Error(`${label} returned an invalid certificate record`);
    }
  }
  return fingerprints;
}

async function exactCertificateIsInstalled(keychain, label, fingerprint256) {
  return (await keychainCertificateFingerprints(keychain, label)).has(fingerprint256);
}

async function verifyExactCertificateTwice(keychain, label, fingerprint256) {
  const first = await exactCertificateIsInstalled(keychain, label, fingerprint256);
  const second = await exactCertificateIsInstalled(keychain, label, fingerprint256);
  if (!first || !second) throw new Error(`exact certificate is not stable in ${label}`);
}

async function importIntoLoginKeychain(file) {
  return await command("/usr/bin/security", [
    "import", file, "-k", LOGIN_KEYCHAIN, "-T", "/usr/bin/codesign",
  ]);
}

async function importCertificate(file, attemptId, authorization) {
  await Promise.all([access(SYSTEM_KEYCHAIN), access(LOGIN_KEYCHAIN)]);
  const fingerprint256 = await certificateFingerprint256(file);
  let systemState = "existing_exact";
  if (!await exactCertificateIsInstalled(SYSTEM_KEYCHAIN, "System keychain", fingerprint256)) {
    const imported = await importIntoSystemKeychain(file, authorization, attemptId);
    if (imported.code !== 0 && !await exactCertificateIsInstalled(
      SYSTEM_KEYCHAIN, "System keychain", fingerprint256,
    )) {
      throw new Error("Apple Distribution certificate import into System keychain failed");
    }
    systemState = "imported_verified";
  }
  await verifyExactCertificateTwice(SYSTEM_KEYCHAIN, "System keychain", fingerprint256);

  let loginState = "existing_exact";
  if (!await exactCertificateIsInstalled(LOGIN_KEYCHAIN, "login keychain", fingerprint256)) {
    const imported = await importIntoLoginKeychain(file);
    if (imported.code !== 0 && !await exactCertificateIsInstalled(
      LOGIN_KEYCHAIN, "login keychain", fingerprint256,
    )) {
      throw new Error("Apple Distribution certificate import into login keychain failed");
    }
    loginState = "imported_verified";
  }
  await verifyExactCertificateTwice(LOGIN_KEYCHAIN, "login keychain", fingerprint256);
  return { fingerprint256, systemState, loginState };
}

async function certificateDetailFromExisting(page, candidates) {
  if (candidates.length > 1) throw new Error("multiple active Apple Distribution web certificates found");
  if (candidates.length !== 1) return null;
  await clickRow(candidates[0].row, "Apple Distribution certificate row");
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "/account/resources/certificates");
  return `existing-certificate-${hashBytes(safeUrl(page.url())).slice(0, 24)}`;
}

async function optionalDownloadedIdentity(teamId, evidence) {
  const identities = await stableIdentityInventory(teamId);
  if (identities.length !== 1) return null;
  return identities[0].sha1 === evidence.fingerprintSha1 ? identities[0] : null;
}

async function existingVisibleDistributionCertificate(teamId) {
  const evidence = await validateCer(VISIBLE_CERTIFICATE_PATH).catch((error) => {
    if (error?.code === "ENOENT") return null;
    throw error;
  });
  if (!evidence) return null;
  if (teamId && !evidence.commonName?.includes(`(${teamId})`)) {
    throw new Error("Downloads/distribution.cer belongs to a different Apple Team");
  }
  return { file: VISIBLE_CERTIFICATE_PATH, evidence, visibleFile: VISIBLE_CERTIFICATE_PATH };
}

async function ensureDistributionCertificate(workflowPage, sessionId, handoff, csr, authorization, run) {
  const page = await openPage(workflowPage, CERTIFICATES_URL, "/account/resources/certificates");
  run.pages.certificates = safeUrl(page.url());
  await writeSecureJson(runPath(sessionId), run);

  const candidates = await certificateCandidates(page, handoff.userName);
  if (candidates.length >= 1) {
    return {
      identity: null,
      attemptId: `existing-certificate-${hashBytes(handoff.userName).slice(0, 24)}`,
      downloaded: null,
      pageOpened: true,
      recovered: true,
      existing: true,
    };
  }

  let existingLedger = await readJson(certificateLedgerPath(sessionId));
  if (existingLedger && new Set(["clicking", "submitted", "unknown"]).has(existingLedger.state)) {
    const recovered = await recoverCertificateDownload(
      sessionId, existingLedger.attemptId, csr,
    );
    if (recovered) {
      await importCertificate(recovered.file, existingLedger.attemptId, authorization);
      const identity = await optionalDownloadedIdentity(handoff.teamId, recovered.evidence);
      if (existingLedger.state !== "submitted") {
        existingLedger = await updateLedger(
          certificateLedgerPath(sessionId), existingLedger, "submitted",
        );
      }
      return {
        identity,
        attemptId: existingLedger.attemptId,
        downloaded: recovered,
        pageOpened: true,
        recovered: true,
      };
    }
  }

  let attemptId;
  if (existingLedger && existingLedger.state !== "planned") {
    if (!new Set(["clicking", "submitted", "unknown"]).has(existingLedger.state)) {
      throw new Error(`unsupported certificate attempt state: ${existingLedger.state}`);
    }
    if (candidates.length !== 1) {
      throw new Error(`certificate attempt is ${existingLedger.state}, but one recoverable certificate is not visible`);
    }
    await certificateDetailFromExisting(page, candidates);
    attemptId = existingLedger.attemptId;
  } else {
    const ledger = await createCertificate(page, sessionId, handoff, csr);
    attemptId = ledger.attemptId;
  }

  const downloaded = await downloadCertificate(page, sessionId, attemptId, csr);
  await importCertificate(downloaded.file, attemptId, authorization);
  const identity = await optionalDownloadedIdentity(handoff.teamId, downloaded.evidence);
  if (existingLedger && existingLedger.state !== "submitted") {
    await updateLedger(certificateLedgerPath(sessionId), existingLedger, "submitted");
  }
  return { identity, attemptId, downloaded, pageOpened: true, recovered: false };
}

async function classifyProfileRows(page, handoff) {
  const rows = await stableTableRows(page, "provisioning profile list");
  const exact = rows.filter(({ cells, text }) =>
    cells.some((value) => value === handoff.profileName) || text.includes(handoff.profileName));
  const sameApp = rows.filter(({ text }) => text.includes(handoff.bundleId));
  const conflicts = sameApp.filter((candidate) => !exact.includes(candidate));
  return { exact, conflicts };
}

function verifyProfileRow(candidate, handoff) {
  const text = candidate.text.replace(/\s+/g, " ").trim();
  const exactName = candidate.cells.some((value) => value === handoff.profileName);
  if (!exactName) throw new Error("existing provisioning profile name is not exact");
  if (!/\biOS\b/i.test(text)) throw new Error("existing provisioning profile platform is not iOS");
  if (!/App Store(?: Connect)?/i.test(text)) throw new Error("existing provisioning profile type is not App Store");
  return true;
}

async function verifyProfileDetail(page, handoff, identity) {
  await waitForSafePage(page, "/account/resources/profiles");
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  const checks = [
    [handoff.profileName, "profile Name"],
    [handoff.bundleId, "profile App ID"],
  ];
  if (handoff.teamId) checks.push([handoff.teamId, "profile Team"]);
  for (const [value, label] of checks) if (!body.includes(value)) throw new Error(`${label} is not verified on the profile page`);
  if (!/App Store(?: Connect)?/i.test(body)) throw new Error("profile Type is not App Store/App Store Connect");
  if (identity?.commonName) {
    const identityName = identity.commonName.replace(/^Apple Distribution:\s*/, "");
    if (!body.includes(identity.commonName) && !body.includes(identityName)) {
      throw new Error("profile certificate is not verified");
    }
  }
  await uniqueRoleAction(page, "Download", "provisioning profile Download");
}

async function openProfileByExactName(page, handoff, identity) {
  const names = await visibleInFrames(
    page,
    (frame) => frame.getByText(handoff.profileName, { exact: true }),
  );
  if (names.length > 1) throw new Error("multiple exact provisioning profile names are visible");
  if (names.length === 0) return false;
  await names[0].click({ timeout: SHORT_TIMEOUT });
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await verifyProfileDetail(page, handoff, identity);
  return true;
}

async function selectAppId(page, bundleId) {
  const bundlePattern = new RegExp(bundleId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const readinessDeadline = Date.now() + 60_000;
  let ready = false;
  while (Date.now() < readinessDeadline) {
    const markers = [
      ...await visibleInFrames(page, (frame) => frame.getByPlaceholder("Select...", { exact: true })),
      ...await visibleInFrames(page, (frame) => frame.getByText("Select...", { exact: true })),
      ...await visibleInFrames(page, (frame) => frame.getByRole("combobox")),
      ...await visibleInFrames(page, (frame) => frame.getByText(bundlePattern)),
    ];
    if (markers.length) {
      ready = true;
      break;
    }
    await page.waitForTimeout(250);
  }
  if (!ready) throw new Error("App ID selector did not load within 60 seconds");

  const selects = await visibleInFrames(page, (frame) => frame.locator("select"));
  const matching = [];
  for (const select of selects) {
    const options = await select.locator("option").allTextContents();
    const indexes = options.map((text, index) => text.includes(bundleId) ? index : -1).filter((index) => index >= 0);
    if (indexes.length === 1) matching.push({ select, index: indexes[0] });
    if (indexes.length > 1) throw new Error("Bundle ID appears multiple times in one App ID selector");
  }
  if (matching.length === 1) {
    const option = matching[0].select.locator("option").nth(matching[0].index);
    await matching[0].select.selectOption(await option.getAttribute("value"));
    if (!(await matching[0].select.locator("option:checked").innerText()).includes(bundleId)) {
      throw new Error("App ID selection did not persist");
    }
    return;
  }

  const customSelectors = [
    ...await visibleInFrames(page, (frame) => frame.getByPlaceholder("Select...", { exact: true })),
    ...await visibleInFrames(page, (frame) => frame.getByText("Select...", { exact: true })),
  ];
  if (customSelectors.length > 1) {
    throw new Error(`custom App ID selector must be unique; found ${customSelectors.length}`);
  }
  if (customSelectors.length === 1) {
    await customSelectors[0].click({ timeout: SHORT_TIMEOUT });
    const optionMatches = await visibleInFrames(page, (frame) => frame.getByText(bundlePattern));
    const leafOptions = [];
    for (const candidate of optionMatches) {
      const isReactOption = await candidate.evaluate((element) =>
        /^react-select-\d+-option-\d+$/.test(element.id) || /(?:^|\s)css-\S+-option(?:\s|$)/.test(String(element.className || "")),
      );
      if (isReactOption) leafOptions.push(candidate);
    }
    if (leafOptions.length !== 1) {
      throw new Error(`exact custom App ID option must be unique; found ${leafOptions.length}`);
    }
    await leafOptions[0].click({ timeout: SHORT_TIMEOUT });
    const selected = await visibleInFrames(page, (frame) => frame.getByText(bundlePattern));
    if (!selected.length) throw new Error("custom App ID selection did not persist");
    return;
  }

  const comboboxes = await visibleInFrames(page, (frame) => frame.getByRole("combobox"));
  if (comboboxes.length > 1) throw new Error(`App ID combobox must be unique; found ${comboboxes.length}`);
  if (comboboxes.length === 1) {
    await comboboxes[0].click({ timeout: SHORT_TIMEOUT });
    const option = await exactlyOne(
      page,
      (frame) => frame.getByRole("option", { name: bundlePattern }),
      "exact App ID option",
    );
    await option.click({ timeout: SHORT_TIMEOUT });
    const selected = await comboboxes[0].evaluate((element) =>
      `${element.value || ""} ${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`,
    );
    if (!selected.includes(bundleId)) throw new Error("App ID combobox selection did not persist");
    return;
  }

  const textMatches = await visibleInFrames(page, (frame) => frame.getByText(bundlePattern));
  if (textMatches.length !== 1) {
    const summaries = [];
    for (const frame of page.frames()) {
      const content = await frame.locator("body").innerText().catch(() => "");
      if (content.trim()) summaries.push(content.replace(/\s+/g, " ").trim().slice(0, 1500));
    }
    throw new Error(`exact App ID is unavailable; visibleProfileState=${JSON.stringify(summaries)}`);
  }
  const [text] = textMatches;
  const row = text.locator("xpath=ancestor::*[self::label or self::tr or self::li][1]");
  const controls = await visible(row.locator('input[type="radio"], input[type="checkbox"]'));
  if (controls.length !== 1) throw new Error("exact App ID control is not unique");
  await controls[0].check({ timeout: SHORT_TIMEOUT });
}

async function selectCertificate(page, identity) {
  const rows = await stableTableRows(page, "Distribution certificate selection");
  const selectableRows = [];
  for (const candidate of rows) {
    const controls = await visible(candidate.row.locator('input[type="radio"], input[type="checkbox"]'));
    if (controls.length === 1) selectableRows.push({ ...candidate, control: controls[0] });
    if (controls.length > 1) throw new Error("a Distribution certificate row contains multiple controls");
  }
  const fullName = identity?.commonName?.replace(/^Apple Distribution:\s*/, "") || null;
  const matches = fullName
    ? selectableRows.filter(({ text }) => text.includes(identity.commonName) || text.includes(fullName))
    : selectableRows;
  if (matches.length !== 1) {
    const summaries = matches.map(({ text }) => text.replace(/\s+/g, " ").trim().slice(0, 300));
    throw new Error(`matching Distribution certificate row must be unique; candidates=${JSON.stringify(summaries)}`);
  }
  await matches[0].control.check({ timeout: SHORT_TIMEOUT });
  if (!await matches[0].control.isChecked()) throw new Error("Distribution certificate selection did not persist");
  return hashJson(matches[0].cells);
}

async function finishProfileGeneration(page, sessionId, handoff, identity, certificateSelectionHash) {
  const ledgerFile = profileLedgerPath(sessionId);
  const nameInput = await exactlyOne(
    page,
    (frame) => frame.getByLabel(/Profile Name/i).or(frame.locator('input[name*="name" i], input[id*="name" i]')),
    "Profile Name input",
  );
  await nameInput.fill(handoff.profileName);
  if ((await nameInput.inputValue()).trim() !== handoff.profileName) throw new Error("Profile Name did not persist");
  const generate = await uniqueRoleAction(page, "Generate", "profile Generate");
  const ledger = await prepareLedger(ledgerFile, {
    kind: "app-store-profile",
    sessionId,
    pageIdentity: { url: safeUrl(page.url()), titleHash: hashBytes(await page.title()) },
    teamHash: handoff.teamId ? hashBytes(handoff.teamId) : null,
    bundleHash: hashBytes(handoff.bundleId),
    profileNameHash: hashBytes(handoff.profileName),
    certificateIdentity: identity?.sha1 || certificateSelectionHash,
  });
  if (ledger.state !== "planned") throw new Error(`profile attempt is ${ledger.state}; refusing to Generate again`);
  const liveSessionId = sessionIdFromVersion(await browserVersion());
  if (liveSessionId !== sessionId) throw new Error("browser session changed before Generate");
  assertAppleUrl(page.url(), "/account/resources/profiles");
  const clicking = await updateLedger(ledgerFile, ledger, "clicking");
  try {
    await generate.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(ledgerFile, clicking, "unknown");
    throw new Error(`profile Generate result is unknown: ${error.message}`);
  }
  try {
    await page.getByText(/Download and Install/i).first().waitFor({ state: "visible", timeout: TIMEOUT });
  } catch {
    await updateLedger(ledgerFile, clicking, "unknown");
    throw new Error("Generate was clicked once, but Download and Install did not appear");
  }
  return await updateLedger(ledgerFile, clicking, "submitted");
}

async function createProfile(page, sessionId, handoff, identity) {
  const ledgerFile = profileLedgerPath(sessionId);
  const existingLedger = await readJson(ledgerFile);
  if (existingLedger && existingLedger.state !== "planned") {
    throw new Error(`profile attempt is ${existingLedger.state}; only read-only recovery is allowed`);
  }
  await clickAdd(page, "add profile action");
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "/account/resources/profiles");
  if (new URL(page.url()).pathname.replace(/\/+$/, "") !== PROFILE_ADD_PATH) {
    throw new Error(`Generate a profile did not open: ${safeUrl(page.url())}`);
  }
  await chooseExactRadio(page, "App Store Connect", "App Store Connect profile type");
  await (await uniqueRoleAction(page, "Continue")).click({ timeout: SHORT_TIMEOUT });
  await selectAppId(page, handoff.bundleId);
  await (await uniqueRoleAction(page, "Continue")).click({ timeout: SHORT_TIMEOUT });
  const certificateSelectionHash = await selectCertificate(page, identity);
  await (await uniqueRoleAction(page, "Continue")).click({ timeout: SHORT_TIMEOUT });
  return await finishProfileGeneration(
    page, sessionId, handoff, identity, certificateSelectionHash,
  );
}

async function archiveUnknownProfileAttempt(sessionId, ledger, handoff) {
  if (ledger?.state !== "unknown") {
    throw new Error(`profile attempt ${ledger?.state || "missing"} cannot be archived as unknown`);
  }
  const archive = archivedProfileLedgerPath(sessionId, ledger.attemptId);
  await mkdir(path.dirname(archive), { recursive: true, mode: 0o700 });
  await chmod(path.dirname(archive), 0o700);
  const existingArchive = await readJson(archive);
  if (existingArchive) {
    if (existingArchive.attemptId !== ledger.attemptId || existingArchive.state !== "unknown") {
      throw new Error("archived profile attempt conflicts with the unknown ledger");
    }
  } else {
    await rename(profileLedgerPath(sessionId), archive);
    await chmod(archive, 0o600);
  }
  await writeSecureJson(profileCorrectionPath(sessionId), {
    kind: "profile-name-correction",
    sessionId,
    previousAttemptId: ledger.attemptId,
    previousProfileNameHash: hashBytes(`${handoff.appName} App Store`),
    correctedProfileNameHash: hashBytes(handoff.appName),
    state: "verified_absent_then_corrected",
    createdAt: new Date().toISOString(),
  });
}

async function auditUnknownProfileAttempt(workflowPage, sessionId, handoff, identity, ledger) {
  const page = await openPage(workflowPage, PROFILES_URL, "/account/resources/profiles");
  const legacyName = `${handoff.appName} App Store`;
  const samples = [];
  let finalRows = [];
  for (let attempt = 0; attempt < 3; attempt += 1) {
    finalRows = await stableTableRows(page, "provisioning profile recovery list");
    samples.push(hashJson(finalRows.map(({ cells }) => cells)));
    if (attempt < 2) await page.waitForTimeout(500);
  }
  if (new Set(samples).size !== 1) throw new Error("Profiles list changed during read-only recovery");

  const legacy = finalRows.filter(({ cells }) => cells.some((value) => value === legacyName));
  const corrected = finalRows.filter(({ cells }) => cells.some((value) => value === handoff.profileName));
  if (legacy.length > 1 || corrected.length > 1) {
    throw new Error("multiple provisioning profiles prevent read-only recovery");
  }
  if (legacy.length === 1) {
    await clickRow(legacy[0].row, "wrong-name provisioning profile row");
    await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
    await waitForSafePage(page, "/account/resources/profiles");
    const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
    if (!body.includes(legacyName) || !body.includes(handoff.bundleId) || !/App Store Connect/i.test(body)) {
      throw new Error("wrong-name profile detail could not be verified");
    }
    await uniqueRoleAction(page, "Download", "wrong-name profile Download");
    await updateLedger(profileLedgerPath(sessionId), ledger, "submitted");
    throw new Error(`WRONG_NAME_PROFILE_GENERATED=${legacyName}`);
  }
  if (corrected.length === 1) {
    throw new Error(`CORRECT_PROFILE_ALREADY_EXISTS=${handoff.profileName}`);
  }
  if (ledger.state !== "unknown") {
    throw new Error(`profile attempt is ${ledger.state}, but the generated profile is not visible`);
  }
  await archiveUnknownProfileAttempt(sessionId, ledger, handoff);
  const submitted = await createProfile(page, sessionId, handoff, identity);
  await verifyProfileDetail(page, handoff, identity);
  return submitted.attemptId;
}

async function ensureProvisioningProfile(workflowPage, sessionId, handoff, identity, run) {
  if (new URL(workflowPage.url()).pathname.replace(/\/+$/, "") === PROFILE_ADD_PATH) {
    await waitForSafePage(workflowPage, "/account/resources/profiles");
    run.pages.profiles = safeUrl(workflowPage.url());
    await writeSecureJson(runPath(sessionId), run);
    const existingLedger = await readJson(profileLedgerPath(sessionId));
    if (existingLedger && existingLedger.state !== "planned") {
      if (existingLedger.state === "submitted") {
        await verifyProfileDetail(workflowPage, handoff, identity);
        return existingLedger.attemptId;
      }
      return await auditUnknownProfileAttempt(
        workflowPage, sessionId, handoff, identity, existingLedger,
      );
    }
    const certificateSelectionHash = await selectCertificate(workflowPage, identity);
    await (await uniqueRoleAction(workflowPage, "Continue")).click({ timeout: SHORT_TIMEOUT });
    const submitted = await finishProfileGeneration(
      workflowPage, sessionId, handoff, identity, certificateSelectionHash,
    );
    await verifyProfileDetail(workflowPage, handoff, identity);
    return submitted.attemptId;
  }

  const page = await openPage(workflowPage, PROFILES_URL, "/account/resources/profiles");
  run.pages.profiles = safeUrl(page.url());
  await writeSecureJson(runPath(sessionId), run);
  const submittedLedger = await readJson(profileLedgerPath(sessionId));

  const classified = await classifyProfileRows(page, handoff);
  const matches = classified.exact;
  if (classified.conflicts.length) throw new Error("a provisioning profile for this App ID conflicts with the target name");
  if (matches.length > 1) throw new Error("multiple provisioning profiles have the exact target name");
  if (matches.length === 1) {
    verifyProfileRow(matches[0], handoff);
    let ledger = await readJson(profileLedgerPath(sessionId));
    if (ledger && ledger.state !== "submitted") ledger = await updateLedger(profileLedgerPath(sessionId), ledger, "submitted");
    return "existing-profile";
  }

  if (submittedLedger?.state === "submitted") {
    throw new Error("submitted provisioning profile is no longer visible");
  }

  const ledger = await readJson(profileLedgerPath(sessionId));
  if (ledger && ledger.state !== "planned") {
    if (ledger.state === "submitted" && await openProfileByExactName(page, handoff, identity)) {
      return ledger.attemptId;
    }
    return await auditUnknownProfileAttempt(page, sessionId, handoff, identity, ledger);
  }
  const submitted = await createProfile(page, sessionId, handoff, identity);
  await verifyProfileDetail(page, handoff, identity);
  return submitted.attemptId;
}

async function runUtm13() {
  const authorization = await readStdinAuthorization();
  try {
    await verifyRequiredCommands();
    const { context, sessionId } = await connectExistingSession();
    const handoff = await readUtm12Handoff(sessionId);
    handoff.userName = String(authorization.certificateUserName || "").trim();
    if (!handoff.userName) throw new Error("Notion certificate user name is missing");
    let run = await readJson(runPath(sessionId));
    const inputSummaryHash = hashJson({
      schemaVersion: 2,
      bundleHash: hashBytes(handoff.bundleId),
      appNameHash: hashBytes(handoff.appName),
      profileNameHash: hashBytes(handoff.profileName),
    });
    if (run && run.sessionId !== sessionId) {
      throw new Error("existing utm-13 run belongs to a different session");
    }
    if (!run) {
      run = {
        runId: randomUUID(),
        sessionId,
        schemaVersion: 2,
        createdAt: new Date().toISOString(),
        state: "started",
        inputSummaryHash,
        checkpoints: {},
        pages: {},
      };
      await writeSecureJson(runPath(sessionId), run);
    } else if (run.inputSummaryHash !== inputSummaryHash) {
      run.previousInputSummaryHashes ||= [];
      if (run.inputSummaryHash && !run.previousInputSummaryHashes.includes(run.inputSummaryHash)) {
        run.previousInputSummaryHashes.push(run.inputSummaryHash);
      }
      run.schemaVersion = 2;
      run.inputSummaryHash = inputSummaryHash;
    }
    run.pages ||= {};
    run.checkpoints ||= {};
    await writeSecureJson(runPath(sessionId), run);

    for (const existingPage of context.pages()) {
      if (!isApprovedApplePage(existingPage)) continue;
      const pathname = new URL(existingPage.url()).pathname;
      if (
        pathname.startsWith("/account/resources/certificates") ||
        pathname.startsWith("/account/resources/profiles")
      ) {
        await existingPage.close().catch(() => {});
      }
    }
    const workflowPage = await context.newPage();
    await workflowPage.goto(CERTIFICATES_URL, {
      waitUntil: "domcontentloaded",
      timeout: TIMEOUT,
    });
    await waitForSafePage(workflowPage, "/account/resources/certificates");
    run.pages.certificates = safeUrl(workflowPage.url());
    await writeSecureJson(runPath(sessionId), run);

    const csr = await validateStableCsr();
    handoff.teamId = await readTeamFromCertificatesPage(workflowPage, handoff);
    const certificate = await ensureDistributionCertificate(
      workflowPage, sessionId, handoff, csr, authorization, run,
    );
    run.checkpoints.certificate = {
      state: "verified",
      attemptId: certificate.attemptId,
      identityVerified: Boolean(certificate.identity),
      updatedAt: new Date().toISOString(),
    };
    await writeSecureJson(runPath(sessionId), run);

    const profileAttemptId = await ensureProvisioningProfile(
      workflowPage, sessionId, handoff, null, run,
    );
    run.checkpoints.profile = {
      state: "verified",
      attemptId: profileAttemptId,
      updatedAt: new Date().toISOString(),
    };
    run.state = "verified";
    run.completedAt = new Date().toISOString();
    await writeSecureJson(runPath(sessionId), run);
    return {
      LOCAL_BROWSER_SESSION: "verified",
      CSR_DISK: "verified",
      CERTIFICATES_PAGE: certificate.pageOpened ? "opened" : "recovered",
      APPLE_DISTRIBUTION_CERT: certificate.recovered ? "recovered" : "installed",
      EXISTING_CERTIFICATE: Boolean(certificate.existing),
      CODESIGN_IDENTITY: certificate.identity ? "verified" : "not_required",
      PROFILES_PAGE: "opened",
      PROFILE_GENERATE_ATTEMPT_ID: profileAttemptId,
      EXISTING_PROFILE: profileAttemptId === "existing-profile",
      PROVISIONING_PROFILE: "generated_or_existing_exact",
      PROVISIONING_PROFILE_DOWNLOAD: "ready",
      UTM_13: "verified",
    };
  } finally {
    authorization.systemKeychainPassword = null;
    authorization.authorizationAttemptId = null;
  }
}

if (process.argv.length !== 2) {
  process.stderr.write("UTM_13_ERROR=Usage: node scripts/utm_13_one.mjs\n", () => process.exit(1));
} else {
  runUtm13().then(
    (result) => process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0)),
    (error) => process.stderr.write(`UTM_13_ERROR=${error.message}\n`, () => process.exit(1)),
  );
}
