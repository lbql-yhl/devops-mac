#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, readdir, rename, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { connectLocalBrowser } from "./session.mjs";

const TIMEOUT = 30_000;
const SHORT_TIMEOUT = 5_000;
const APP_DETAIL_TIMEOUT = 30_000;
const REGISTER_TRANSITION_TIMEOUT = 90_000;
const CREATE_TRANSITION_TIMEOUT = 3_000;
const DEVELOPER_ACCOUNT_URL = "https://developer.apple.com/account/";
const IDENTIFIERS_URL = "https://developer.apple.com/account/resources/identifiers/list";
const BUNDLE_ID_ADD_URL = "https://developer.apple.com/account/resources/identifiers/bundleId/add/bundle";
const APPS_URL = "https://appstoreconnect.apple.com/apps";
const PROJECT_ROOT = process.cwd();
const RUNTIME_ROOT = path.join(PROJECT_ROOT, "runtime", "utm-12");
const PROFILE_DIR = process.env.LOCAL_BROWSER_PROFILE_DIR ||
  path.join("/tmp", "edge-debug-profile");
const STATE_PATH = process.env.LOCAL_BROWSER_STATE_FILE ||
  path.join(os.homedir(), ".codex", "browser-profiles", "apple-developer.session.json");
const CDP_PORT = Number(process.env.LOCAL_BROWSER_CDP_PORT || "9222");

// Shared URL, hashing, and secure runtime state.
function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function comparableUrl(value) {
  const url = new URL(value);
  const pathname = url.pathname.replace(/\/+$/, "") || "/";
  return `${url.protocol}//${url.host}${pathname}`;
}

function assertAppleUrl(value, hostname) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== hostname) {
    throw new Error(`Unexpected page: ${safeUrl(value)}`);
  }
  return url;
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

function sha256(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function sessionRuntimeDir(sessionOrRun) {
  const sessionId = typeof sessionOrRun === "string" ? sessionOrRun : sessionOrRun.sessionId;
  if (!/^[a-f0-9-]{20,}$/i.test(sessionId)) throw new Error("invalid browser session ID");
  return path.join(RUNTIME_ROOT, sessionId);
}

function runPath(sessionOrRun) {
  return path.join(sessionRuntimeDir(sessionOrRun), "run.json");
}

function ledgerPath(sessionOrRun, kind) {
  return path.join(sessionRuntimeDir(sessionOrRun), `${kind}-attempt.json`);
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

async function findVerifiedUtm11Run(sessionId) {
  const candidates = [
    path.join(PROJECT_ROOT, "runtime", "utm-11", sessionId, "run.json"),
    path.join(PROJECT_ROOT, "runtime", "utm-11", "run.json"),
  ];
  for (const candidate of candidates) {
    const run = await readJson(candidate);
    if (run?.state === "verified" && run?.sessionId === sessionId) return run;
  }
  return null;
}

async function findPriorVerifiedUtm11Run() {
  const root = path.join(PROJECT_ROOT, "runtime", "utm-11");
  const legacy = await readJson(path.join(root, "run.json"));
  if (legacy?.state === "verified" && legacy?.sessionId) return legacy;
  const entries = await readdir(root, { withFileTypes: true }).catch((error) => {
    if (error?.code === "ENOENT") return [];
    throw error;
  });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const candidate = await readJson(path.join(root, entry.name, "run.json"));
    if (candidate?.state === "verified" && candidate?.sessionId) return candidate;
  }
  return null;
}

async function adoptCurrentAuthenticatedSession(context, sessionId) {
  const prior = await findPriorVerifiedUtm11Run();
  if (!prior) throw new Error("current-session adoption requires a prior verified UTM-11 run");
  const identifierPages = context.pages().filter((page) => {
    try {
      const url = new URL(page.url());
      return url.protocol === "https:"
        && url.hostname === "developer.apple.com"
        && url.pathname.startsWith("/account/resources/identifiers");
    } catch {
      return false;
    }
  });
  if (identifierPages.length !== 1) {
    throw new Error(`current-session adoption requires exactly one Identifiers page; found ${identifierPages.length}`);
  }
  const page = identifierPages[0];
  await waitForSafePage(page, "developer.apple.com");
  const loginFields = await visibleInFrames(page, (frame) => frame.locator(
    "input#account_name_text_field, input[autocomplete~='username'], input[type='password']",
  ));
  if (loginFields.length) throw new Error("current-session adoption found an Apple login form");
  const title = await page.title();
  if (!/Certificates, Identifiers\s*&\s*Profiles/i.test(title)) {
    throw new Error("current-session adoption could not verify the authenticated Identifiers page");
  }
  const now = new Date().toISOString();
  const adopted = {
    runId: randomUUID(),
    sessionId,
    createdAt: now,
    completedAt: now,
    state: "verified",
    adoptedFor: "utm-12-identifiers-recovery",
    priorVerifiedSessionHash: sha256(prior.sessionId),
    pages: { identifiers: safeUrl(page.url()) },
  };
  await writeSecureJson(
    path.join(PROJECT_ROOT, "runtime", "utm-11", sessionId, "run.json"),
    adopted,
  );
  return adopted;
}

async function connectExistingUtm11Session({ adoptCurrentSession = false } = {}) {
  const [info, state] = await Promise.all([browserVersion(), readJson(STATE_PATH)]);
  const liveSessionId = sessionIdFromVersion(info);
  if (!info || !state || !liveSessionId) {
    throw new Error("utm-11 browser is not running; refusing to start another browser");
  }
  if (state.sessionId !== liveSessionId) throw new Error("utm-11 browser session changed");

  const connected = await connectLocalBrowser();
  if (connected.sessionId !== liveSessionId) {
    throw new Error("connected browser session does not match utm-11");
  }

  let verifiedUtm11 = await findVerifiedUtm11Run(liveSessionId);
  if (!verifiedUtm11 && adoptCurrentSession) {
    verifiedUtm11 = await adoptCurrentAuthenticatedSession(connected.context, liveSessionId);
  }
  if (!verifiedUtm11) throw new Error("UTM_11 is not verified for the live browser session");

  const existingRun = await readJson(runPath(liveSessionId));
  const run = existingRun || {
    runId: randomUUID(),
    sessionId: liveSessionId,
    createdAt: new Date().toISOString(),
    state: "started",
  };
  if (!existingRun) await writeSecureJson(runPath(run), run);
  return { context: connected.context, run };
}

async function verifyWorkflowPreflight(context, run) {
  await assertLiveSession(run);
  const secureFiles = [
    runPath(run),
    ledgerPath(run, "agreement"),
    ledgerPath(run, "app-id-register"),
    ledgerPath(run, "app-store-connect-agreement"),
    ledgerPath(run, "app-create"),
  ];
  for (const file of secureFiles) {
    const metadata = await stat(file).catch((error) => {
      if (error?.code === "ENOENT") return null;
      throw error;
    });
    if (metadata && (metadata.mode & 0o777) !== 0o600) {
      throw new Error(`preflight secure file mode must be 600: ${path.basename(file)}`);
    }
  }
  const pages = context.pages();
  if (pages.length !== 1 || !isApprovedApplePage(pages[0])) {
    throw new Error(`preflight requires exactly one approved Apple browser tab; found ${pages.length}`);
  }
  await verifyBeforeNavigation(pages[0]);
  return pages[0];
}

// Application input is resolved only after the Developer Account checkpoint.
function requireString(value, label) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`utm-12 config is missing ${label}`);
  return value.trim();
}

function validateBundleId(value) {
  const bundleId = requireString(value, "bundleId");
  if (!/^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/.test(bundleId) || bundleId.includes("*")) {
    throw new Error("utm-12 config bundleId must be an explicit Bundle ID");
  }
  return bundleId;
}

async function loadAppConfig() {
  let stdinText = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) stdinText += chunk;
  if (!stdinText.trim()) throw new Error("utm-12 requires application JSON on stdin");
  const payload = JSON.parse(stdinText);
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("utm-12 stdin must be a JSON object");
  }
  const allowed = new Set(["APP_NAME", "BUNDLE_ID", "RESET_FROM_IDENTIFIERS"]);
  for (const key of Object.keys(payload)) {
    if (!allowed.has(key)) throw new Error(`utm-12 stdin contains unsupported field: ${key}`);
  }
  const input = {
    appName: payload.APP_NAME,
    bundleId: payload.BUNDLE_ID,
    description: payload.APP_NAME,
    platform: "iOS",
    primaryLanguage: "English (U.S.)",
    sku: payload.BUNDLE_ID,
    accessLevel: "Full Access",
  };
  const config = {
    appName: requireString(input.appName, "appName"),
    bundleId: input.bundleId ? validateBundleId(input.bundleId) : null,
    description: requireString(input.description || input.appName, "description"),
    platform: requireString(input.platform || "iOS", "platform"),
    primaryLanguage: input.primaryLanguage ? requireString(input.primaryLanguage, "primaryLanguage") : "English (U.S.)",
    sku: input.sku ? requireString(input.sku, "sku") : null,
    accessLevel: input.accessLevel ? requireString(input.accessLevel, "accessLevel") : "Full Access",
    resetFromIdentifiers: payload.RESET_FROM_IDENTIFIERS === true,
  };
  if (payload.RESET_FROM_IDENTIFIERS !== undefined
    && payload.RESET_FROM_IDENTIFIERS !== true) {
    throw new Error("RESET_FROM_IDENTIFIERS must be true when provided");
  }
  if (config.platform !== "iOS") throw new Error("utm-12 currently requires platform to be exactly iOS");
  if (!config.resetFromIdentifiers) {
    await writeSecureJson(path.join(RUNTIME_ROOT, "input.json"), {
      APP_NAME: config.appName,
      BUNDLE_ID: config.bundleId,
    });
  }
  return config;
}

async function resetFromIdentifiers(run, config) {
  const previousInput = await readJson(path.join(RUNTIME_ROOT, "input.json"));
  if (!previousInput || previousInput.BUNDLE_ID !== config.bundleId) {
    throw new Error("identifier restart Bundle ID does not match the previous input");
  }
  if (previousInput.APP_NAME === config.appName) {
    throw new Error("identifier restart requires an explicitly changed app name");
  }
  if (run.state === "verified") {
    throw new Error("identifier restart refuses a verified UTM-12 run");
  }

  const appCreate = await readJson(ledgerPath(run, "app-create"));
  const safePlanned = appCreate?.state === "planned"
    && appCreate.recoveryEvidence === "app-name-fallback";
  const safeRejected = appCreate?.state === "rejected"
    && appCreate.rejectionEvidence === "app-name-already-used";
  if (appCreate && !safePlanned && !safeRejected) {
    throw new Error(`identifier restart refuses App Create state ${appCreate.state}`);
  }

  const archive = async (kind, ledger) => {
    if (!ledger) return;
    const suffix = sha256({
      kind,
      attemptId: ledger.attemptId,
      previousAppName: previousInput.APP_NAME,
      nextAppName: config.appName,
    }).slice(0, 16);
    const target = path.join(
      sessionRuntimeDir(run),
      `${kind}-attempt.superseded-${suffix}.json`,
    );
    await rename(ledgerPath(run, kind), target);
  };
  const appId = await readJson(ledgerPath(run, "app-id-register"));
  await archive("app-id-register", appId);
  await archive("app-create", appCreate);

  const checkpoints = { ...(run.checkpoints || {}) };
  delete checkpoints.appId;
  delete checkpoints.appIdExisting;
  delete checkpoints.appStoreApp;
  run.checkpoints = checkpoints;
  run.state = "started";
  delete run.result;
  run.resetFrom = "identifiers";
  run.resetAt = new Date().toISOString();
  run.previousAppNameHash = sha256(previousInput.APP_NAME);
  run.currentAppNameHash = sha256(config.appName);
  await writeSecureJson(runPath(run), run);
  await writeSecureJson(path.join(RUNTIME_ROOT, "input.json"), {
    APP_NAME: config.appName,
    BUNDLE_ID: config.bundleId,
  });
}

// Single-tab Playwright location and navigation helpers.
async function visible(locator) {
  const items = [];
  for (let index = 0; index < await locator.count(); index += 1) {
    const item = locator.nth(index);
    if (await item.isVisible().catch(() => false)) items.push(item);
  }
  return items;
}

async function visibleInFrames(page, locatorFactory) {
  const items = [];
  for (const frame of page.frames()) {
    try {
      items.push(...await visible(locatorFactory(frame)));
    } catch {
      // Ignore a frame that navigated while its elements were being read.
    }
  }
  return items;
}

async function exactlyOne(page, locatorFactory, label, { enabled = false } = {}) {
  const items = await visibleInFrames(page, locatorFactory);
  if (items.length !== 1) throw new Error(`${label} must be uniquely visible; found ${items.length}`);
  if (enabled && !await items[0].isEnabled().catch(() => false)) throw new Error(`${label} is disabled`);
  return items[0];
}

async function waitForUniqueVisibleInFrames(page, locatorFactory, label) {
  const deadline = Date.now() + TIMEOUT;
  while (true) {
    const items = await visibleInFrames(page, locatorFactory);
    if (items.length > 1) {
      throw new Error(`${label} must be uniquely visible; found ${items.length}`);
    }
    if (items.length === 1) return items[0];
    if (Date.now() >= deadline) {
      throw new Error(`${label} must be uniquely visible; found 0`);
    }
    await page.waitForTimeout(20);
  }
}

async function waitForSafePage(page, hostname) {
  assertAppleUrl(page.url(), hostname);
  await page.locator("body").waitFor({ state: "visible", timeout: TIMEOUT });
  if ((await visibleInFrames(page, (frame) => frame.getByText(
    /captcha|verify you are human|unusual activity|security challenge/i,
  ))).length) {
    throw new Error("Apple security challenge detected");
  }
}

async function verifyBeforeNavigation(page) {
  if (!isApprovedApplePage(page)) throw new Error("workflow page is outside approved Apple domains");
  const current = new URL(page.url());
  await page.bringToFront();
  await waitForSafePage(page, current.hostname);
}

async function openPage(
  context,
  run,
  key,
  url,
  hostname,
  reusePage = null,
  { acceptedPathPrefix = null } = {},
) {
  const targetUrl = comparableUrl(url);
  const isAcceptedTarget = (candidate) => {
    try {
      if (comparableUrl(candidate.url()) === targetUrl) return true;
      if (!acceptedPathPrefix) return false;
      const current = new URL(candidate.url());
      return current.protocol === "https:"
        && current.hostname === hostname
        && current.pathname.startsWith(acceptedPathPrefix);
    } catch {
      return false;
    }
  };
  const targetPages = context.pages().filter((candidate) => {
    return isAcceptedTarget(candidate);
  });

  let page = reusePage;
  if (page?.isClosed()) throw new Error("single workflow page was closed");
  if (!page) page = targetPages[0] || context.pages().findLast(isApprovedApplePage) || null;
  if (!page) throw new Error("the verified single workflow tab is unavailable");
  await verifyBeforeNavigation(page);

  if (!isAcceptedTarget(page)) {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  await page.bringToFront();
  await waitForSafePage(page, hostname);
  const busy = page.locator("[aria-busy='true'], [role='progressbar']");
  await busy.first().waitFor({ state: "hidden", timeout: SHORT_TIMEOUT }).catch(() => {});
  await waitForSafePage(page, hostname);

  const finalUrl = comparableUrl(page.url());
  let duplicatePagesClosed = 0;
  for (const candidate of context.pages()) {
    if (candidate === page || candidate.isClosed()) continue;
    let isDuplicate = false;
    try {
      const candidateUrl = comparableUrl(candidate.url());
      isDuplicate = isAcceptedTarget(candidate) || candidateUrl === finalUrl;
    } catch {
      isDuplicate = false;
    }
    if (!isDuplicate) continue;
    await candidate.close();
    duplicatePagesClosed += 1;
  }

  run.pages = { ...(run.pages || {}), [key]: safeUrl(page.url()) };
  run.workflowPage = {
    single: true,
    lastVerifiedPage: key,
    lastDuplicatePagesClosed: duplicatePagesClosed,
    duplicatePagesClosed: (run.workflowPage?.duplicatePagesClosed || 0) + duplicatePagesClosed,
  };
  await writeSecureJson(runPath(run), run);
  return page;
}

// Irreversible-action ledgers. Only planned may advance to one click.
async function prepareLedger(
  run,
  kind,
  page,
  inputSummary,
  { allowPlannedInputMigration = false } = {},
) {
  const file = ledgerPath(run, kind);
  const inputSummaryHash = sha256(inputSummary);
  const existing = await readJson(file);
  if (existing) {
    if (existing.sessionId !== run.sessionId) {
      throw new Error(`existing ${kind} ledger conflicts with this session or input`);
    }
    if (existing.inputSummaryHash !== inputSummaryHash) {
      if (
        allowPlannedInputMigration
        && existing.sessionId === run.sessionId
        && existing.state === "planned"
      ) {
        const migrated = {
          ...existing,
          inputSummaryHash,
          previousInputSummaryHash: existing.inputSummaryHash,
          inputSummaryMigratedAt: new Date().toISOString(),
        };
        await writeSecureJson(file, migrated);
        return migrated;
      }
      throw new Error(`existing ${kind} ledger conflicts with this session or input`);
    }
    return existing;
  }
  const details = {
    kind,
    sessionId: run.sessionId,
    pageIdentity: { url: safeUrl(page.url()), titleHash: sha256(await page.title()) },
    inputSummaryHash,
  };
  const ledger = {
    attemptId: `${kind}-${sha256(details).slice(0, 24)}`,
    ...details,
    state: "planned",
    createdAt: new Date().toISOString(),
  };
  await writeSecureJson(file, ledger);
  return ledger;
}

async function updateLedger(run, ledger, state) {
  const updated = { ...ledger, state, updatedAt: new Date().toISOString() };
  await writeSecureJson(ledgerPath(run, ledger.kind), updated);
  return updated;
}

async function recordExistingLedger(run, kind, page, inputSummary) {
  const ledger = await prepareLedger(run, kind, page, inputSummary);
  if (ledger.state === "submitted") return ledger;
  if (ledger.state !== "planned") return await updateLedger(run, ledger, "submitted");
  return await updateLedger(run, ledger, "submitted");
}

async function assertLiveSession(run) {
  const liveSessionId = sessionIdFromVersion(await browserVersion());
  if (liveSessionId !== run.sessionId) throw new Error("browser session changed before irreversible action");
}

// Agreements are intercepted at every relevant stage, not only at fixed URLs.
async function agreementNeeded(page) {
  const candidates = await visibleInFrames(page, (frame) => frame.getByRole("button", {
    name: /^(Accept|Agree|I Agree|Accept Agreement)$/i,
  }));
  if (candidates.length > 1) throw new Error("multiple agreement buttons are visible");
  return candidates[0] || null;
}

async function openDeveloperAgreementReview(page, run) {
  const candidates = [
    ...await visibleInFrames(
      page,
      (frame) => frame.getByRole("button", { name: "Review agreement", exact: true }),
    ),
    ...await visibleInFrames(
      page,
      (frame) => frame.getByRole("link", { name: "Review agreement", exact: true }),
    ),
  ];
  if (candidates.length > 1) throw new Error("Review agreement control must be unique");
  if (!candidates.length) return false;
  const review = candidates[0];
  if (!await review.isEnabled()) throw new Error("Review agreement control is disabled");
  await assertLiveSession(run);
  assertAppleUrl(page.url(), "developer.apple.com");
  await review.click({ timeout: SHORT_TIMEOUT });
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "developer.apple.com");
  return true;
}

async function ensureAgreement(page, run) {
  let button = await agreementNeeded(page);
  if (!button) {
    await openDeveloperAgreementReview(page, run);
    button = await agreementNeeded(page);
  }
  if (!button) {
    const existing = await readJson(ledgerPath(run, "agreement"));
    if (existing && ["clicking", "unknown"].includes(existing.state)) {
      const submitted = await updateLedger(run, existing, "submitted");
      return submitted.attemptId;
    }
    return existing?.attemptId || "existing";
  }
  const agreementText = (await page.locator("body").innerText()).replace(/\s+/g, " ").trim();
  let ledger = await prepareLedger(run, "agreement", page, { agreementHash: sha256(agreementText) });
  if (ledger.state !== "planned") {
    if (await agreementNeeded(page)) {
      throw new Error(`agreement attempt is ${ledger.state}; refusing to click again`);
    }
    return ledger.attemptId;
  }
  await assertLiveSession(run);
  button = await agreementNeeded(page);
  if (!button || !await button.isEnabled()) throw new Error("agreement button is not uniquely enabled");
  const liveAgreementText = (await page.locator("body").innerText()).replace(/\s+/g, " ").trim();
  if (sha256({ agreementHash: sha256(liveAgreementText) }) !== ledger.inputSummaryHash) {
    throw new Error("agreement page identity changed before acceptance");
  }
  ledger = await updateLedger(run, ledger, "clicking");
  try {
    await button.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(run, ledger, "unknown");
    throw new Error(`agreement click result is unknown: ${error.message}`);
  }
  await Promise.race([
    button.waitFor({ state: "hidden", timeout: TIMEOUT }),
    page.waitForURL((url) => !url.pathname.includes("/account/agree/"), { timeout: TIMEOUT }),
  ]).catch(() => {});
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "developer.apple.com");
  if (await agreementNeeded(page)) {
    await updateLedger(run, ledger, "unknown");
    throw new Error("agreement was clicked once but acceptance could not be verified");
  }
  await updateLedger(run, ledger, "submitted");
  return ledger.attemptId;
}

async function checkboxStatement(checkbox) {
  return await checkbox.evaluate((element) => {
    const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
    const explicit = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
    if (explicit) return normalize(explicit.textContent);
    const wrapping = element.closest("label");
    if (wrapping) return normalize(wrapping.textContent);
    const ariaLabel = normalize(element.getAttribute("aria-label"));
    if (ariaLabel) return ariaLabel;
    const labelledBy = String(element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const labelledText = normalize(labelledBy.map((id) => document.getElementById(id)?.textContent || "").join(" "));
    if (labelledText) return labelledText;
    let ancestor = element.parentElement;
    for (let depth = 0; ancestor && depth < 4; depth += 1, ancestor = ancestor.parentElement) {
      if (ancestor.querySelectorAll("input[type='checkbox']").length !== 1) continue;
      const text = normalize(ancestor.textContent);
      if (text) return text;
    }
    return "";
  });
}

async function appStoreAgreementControls(page) {
  let url;
  try { url = new URL(page.url()); } catch { return { terms: [], checkboxes: [], agrees: [] }; }
  if (url.hostname !== "appstoreconnect.apple.com") {
    return { terms: [], checkboxes: [], agrees: [] };
  }
  const [terms, allCheckboxes, agrees] = await Promise.all([
    visibleInFrames(page, (frame) => frame.getByText("Terms Of Service", { exact: true })),
    visibleInFrames(page, (frame) => frame.locator("input[type='checkbox']")),
    visibleInFrames(page, (frame) => frame.getByRole("button", { name: "Agree", exact: true })),
  ]);
  const statementCheckboxes = [];
  for (const checkbox of allCheckboxes) {
    const statement = await checkboxStatement(checkbox).catch(() => "");
    if (/(?:\bread\b.{0,160}\bagree\b.{0,160}\bterms?\b|\bagree\b.{0,160}\bterms?\b|\bterms?\b.{0,160}\bpresented\b)/i.test(statement)) {
      statementCheckboxes.push(checkbox);
    }
  }
  const checkboxes = statementCheckboxes.length > 0
    ? statementCheckboxes
    : (allCheckboxes.length === 1 ? allCheckboxes : []);
  for (const [label, controls] of [
    ["Terms Of Service", terms],
    ["agreement checkbox", checkboxes],
    ["Agree", agrees],
  ]) {
    if (controls.length > 1) {
      throw new Error(`App Store Connect ${label} must be uniquely visible; found ${controls.length}`);
    }
  }
  return { terms, checkboxes, agrees };
}

async function appStoreAgreementPresent(page) {
  const { terms, checkboxes, agrees } = await appStoreAgreementControls(page);
  return terms.length === 1 && checkboxes.length === 1 && agrees.length === 1;
}

async function appStoreAppsReady(page) {
  let url;
  try { url = new URL(page.url()); } catch { return false; }
  if (url.hostname !== "appstoreconnect.apple.com"
    || url.searchParams.has("rpf")
    || !(url.pathname === "/apps" || url.pathname.startsWith("/apps/"))) {
    return false;
  }
  const controls = await appStoreAgreementControls(page);
  if (controls.terms.length || controls.checkboxes.length || controls.agrees.length) return false;
  const markers = await Promise.all([
    visibleInFrames(page, (frame) => frame.getByRole("heading", { name: /^(My Apps|Apps)$/i })),
    visibleInFrames(page, (frame) => frame.locator(
      "main input[type='search'], main input[placeholder*='Search' i], input[type='search']",
    )),
    visibleInFrames(page, (frame) => frame.getByRole("button", { name: "Add Apps", exact: true })),
    visibleInFrames(page, (frame) => frame.getByRole("button", { name: "New App", exact: true })),
    visibleInFrames(page, (frame) => frame.locator("a[href*='/apps/'][href*='/distribution']")),
    visibleInFrames(page, (frame) => frame.getByText(/iOS(?: App)?(?: Version)?\s+1\.0\b/i)),
  ]);
  return markers.some((items) => items.length > 0);
}

async function waitForAppStoreState(page) {
  const deadline = Date.now() + TIMEOUT;
  while (true) {
    if (await appStoreAgreementPresent(page)) return "agreement";
    if (await appStoreAppsReady(page)) return "apps";
    if (Date.now() >= deadline) return "unknown";
    await page.waitForTimeout(20);
  }
}

async function appStoreAgreementComplete(page) {
  await waitForSafePage(page, "appstoreconnect.apple.com");
  return await appStoreAppsReady(page);
}

async function recoverAppStoreAgreement(page) {
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    if (await appStoreAgreementComplete(page)) return true;
    await page.waitForTimeout(20);
  }
  return await appStoreAgreementComplete(page);
}

async function prepareAppStoreAgreementCheckbox(page, checkbox) {
  const scrollableCount = await checkbox.evaluate((control) => {
    const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
    const controlRect = control.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll("*")).filter((candidate) => {
      if (!(candidate instanceof HTMLElement) || candidate === control || candidate.contains(control)) {
        return false;
      }
      const rect = candidate.getBoundingClientRect();
      const style = getComputedStyle(candidate);
      return rect.width >= 100
        && rect.height >= 80
        && rect.bottom <= controlRect.top + 4
        && rect.bottom > controlRect.top - Math.max(window.innerHeight, 800)
        && candidate.scrollHeight > candidate.clientHeight
        && /^(auto|scroll)$/.test(style.overflowY)
        && /terms\s+of\s+service/i.test(normalize(candidate.innerText));
    });
    if (candidates.length === 1) {
      const candidate = candidates[0];
      candidate.scrollTop = candidate.scrollHeight;
      candidate.dispatchEvent(new Event("scroll", { bubbles: true }));
    }
    return candidates.length;
  });
  if (scrollableCount !== 1) {
    throw new Error(
      `App Store Connect agreement scroll container must be unique; found ${scrollableCount}`,
    );
  }

  const deadline = Date.now() + TIMEOUT;
  while (!await checkbox.isEnabled()) {
    if (Date.now() >= deadline) {
      throw new Error("App Store Connect agreement checkbox did not enable after scrolling");
    }
    await page.waitForTimeout(50);
  }
}

async function clickAppStoreAgreementCheckbox(page, checkbox) {
  const label = checkbox.locator("xpath=ancestor::label[1]");
  if (await label.count() !== 1) {
    throw new Error("App Store Connect agreement checkbox label must be unique");
  }
  const targets = await visible(label.locator("div[type='checkbox']"));
  if (targets.length !== 1) {
    throw new Error(
      `App Store Connect visual agreement checkbox must be unique; found ${targets.length}`,
    );
  }
  await targets[0].click({ timeout: SHORT_TIMEOUT });

  const deadline = Date.now() + TIMEOUT;
  while (!await checkbox.isChecked()) {
    if (Date.now() >= deadline) {
      throw new Error("App Store Connect agreement checkbox did not persist");
    }
    await page.waitForTimeout(20);
  }
}

async function appStoreAgreementDocumentText(checkbox) {
  const result = await checkbox.evaluate((control) => {
    const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
    const controlRect = control.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll("*")).filter((candidate) => {
      if (!(candidate instanceof HTMLElement) || candidate === control || candidate.contains(control)) {
        return false;
      }
      const rect = candidate.getBoundingClientRect();
      const style = getComputedStyle(candidate);
      return rect.width >= 100
        && rect.height >= 80
        && rect.bottom <= controlRect.top + 4
        && rect.bottom > controlRect.top - Math.max(window.innerHeight, 800)
        && candidate.scrollHeight > candidate.clientHeight
        && /^(auto|scroll)$/.test(style.overflowY)
        && /terms\s+of\s+service/i.test(normalize(candidate.innerText));
    });
    return {
      count: candidates.length,
      text: candidates.length === 1 ? normalize(candidates[0].innerText) : "",
    };
  });
  if (result.count !== 1) {
    throw new Error(
      `App Store Connect agreement document must be unique; found ${result.count}`,
    );
  }
  if (!result.text) throw new Error("App Store Connect agreement document is empty");
  return result.text;
}

async function ensureAppStoreAgreement(page, run) {
  let controls = await appStoreAgreementControls(page);
  let accept = controls.terms.length === 1 && controls.checkboxes.length === 1 && controls.agrees.length === 1
    ? controls.agrees[0]
    : null;
  const existingLedger = await readJson(ledgerPath(run, "app-store-connect-agreement"));
  if (!accept) {
    if (!await appStoreAgreementComplete(page)) {
      throw new Error("App Store Connect agreement is absent but Apps page is not verified");
    }
    if (existingLedger && existingLedger.state !== "submitted") {
      await updateLedger(run, existingLedger, "submitted");
    }
    return existingLedger?.attemptId || "existing";
  }

  const checkbox = controls.checkboxes[0];
  const statement = await checkboxStatement(checkbox);
  const agreementText = await appStoreAgreementDocumentText(checkbox);
  if (!/(agreement|terms and conditions|terms of service)/i.test(agreementText)) {
    throw new Error("App Store Connect page does not contain an agreement declaration");
  }
  const checkboxIdentity = statement || "unique-checkbox-on-app-store-connect-agreement-page";
  let ledger = await prepareLedger(run, "app-store-connect-agreement", page, {
    agreementHash: sha256(agreementText),
    checkboxStatementHash: sha256(checkboxIdentity),
    checkboxRequired: true,
  }, {
    allowPlannedInputMigration: true,
  });

  if (ledger.state !== "planned") {
    if (!await recoverAppStoreAgreement(page)) {
      throw new Error(`App Store Connect agreement attempt is ${ledger.state}; refusing to accept again`);
    }
    if (ledger.state !== "submitted") ledger = await updateLedger(run, ledger, "submitted");
    return ledger.attemptId;
  }

  if (!await checkbox.isChecked()) {
    await prepareAppStoreAgreementCheckbox(page, checkbox);
    await clickAppStoreAgreementCheckbox(page, checkbox);
  }
  if (!await checkbox.isChecked()) throw new Error("App Store Connect agreement checkbox did not persist");
  controls = await appStoreAgreementControls(page);
  if (controls.terms.length !== 1 || controls.checkboxes.length !== 1 || controls.agrees.length !== 1) {
    throw new Error("App Store Connect agreement controls changed after checking the agreement");
  }
  accept = controls.agrees[0];
  if (!accept || !await accept.isEnabled()) throw new Error("App Store Connect Accept button is not uniquely enabled");
  const liveText = await appStoreAgreementDocumentText(controls.checkboxes[0]);
  const liveStatement = await checkboxStatement(controls.checkboxes[0]);
  if (sha256({
    agreementHash: sha256(liveText),
    checkboxStatementHash: sha256(liveStatement || "unique-checkbox-on-app-store-connect-agreement-page"),
    checkboxRequired: true,
  }) !== ledger.inputSummaryHash) {
    throw new Error("App Store Connect agreement identity changed before acceptance");
  }
  await assertLiveSession(run);
  assertAppleUrl(page.url(), "appstoreconnect.apple.com");
  ledger = await updateLedger(run, ledger, "clicking");
  try {
    await accept.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(run, ledger, "unknown");
    throw new Error(`App Store Connect Accept click result is unknown: ${error.message}`);
  }
  if (!await recoverAppStoreAgreement(page)) {
    await updateLedger(run, ledger, "unknown");
    throw new Error("App Store Connect Accept was clicked once but Apps access was not verified");
  }
  await updateLedger(run, ledger, "submitted");
  return ledger.attemptId;
}

async function developerAgreementPresent(page) {
  let url;
  try { url = new URL(page.url()); } catch { return false; }
  if (url.hostname !== "developer.apple.com") return false;
  const [updated, reviewButtons, reviewLinks, finalButtons] = await Promise.all([
    visibleInFrames(page, (frame) => frame.getByText(
      /The program license agreement has been updated|Accept the latest Apple Developer Program License Agreement/i,
    )),
    visibleInFrames(page, (frame) => frame.getByRole("button", { name: "Review agreement", exact: true })),
    visibleInFrames(page, (frame) => frame.getByRole("link", { name: "Review agreement", exact: true })),
    agreementNeeded(page),
  ]);
  return updated.length > 0 || reviewButtons.length > 0 || reviewLinks.length > 0
    || (url.pathname.startsWith("/account/agree/") && Boolean(finalButtons));
}

async function handleDeveloperAgreementIfPresent(page, run) {
  if (!await developerAgreementPresent(page)) return null;
  const attemptId = await ensureAgreement(page, run);
  if (await developerAgreementPresent(page)) {
    throw new Error("Apple Developer agreement remains visible after the single acceptance attempt");
  }
  return attemptId;
}

async function handleAppStoreAgreementIfPresent(page, run) {
  const state = await waitForAppStoreState(page);
  if (state === "apps") return null;
  if (state !== "agreement") {
    throw new Error("App Store Connect did not reach Terms Of Service or a verified Apps page");
  }
  const attemptId = await ensureAppStoreAgreement(page, run);
  if (await waitForAppStoreState(page) !== "apps") {
    throw new Error("App Store Connect Terms Of Service remains visible after the single acceptance attempt");
  }
  return attemptId;
}

// Stage 1: Developer Account agreement and membership verification.
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

async function readMembership(page) {
  const deadline = Date.now() + TIMEOUT;
  do {
    for (const frame of page.frames()) {
      const body = await frame.locator("body").innerText().catch(() => "");
      const team = body.match(/Team ID\s+([A-Z0-9]{10})\b/);
      const renewal = body.match(/Renewal date\s+([^\n\r]+)/i);
      if (team && renewal && renewal[1].trim()) {
        return { teamId: team[1], renewalDate: renewal[1].trim() };
      }
    }
    await page.waitForTimeout(20);
  } while (Date.now() < deadline);
  throw new Error("Membership details are unavailable");
}

async function verifyDeveloperAccountStage(context, run) {
  const page = await openPage(
    context,
    run,
    "developerAccount",
    DEVELOPER_ACCOUNT_URL,
    "developer.apple.com",
    null,
    { acceptedPathPrefix: "/account/agree/" },
  );
  const agreementAttemptId = await handleDeveloperAgreementIfPresent(page, run) || "existing";
  if (await agreementNeeded(page)) throw new Error("Developer agreement acceptance is not verified");
  const membership = await readMembership(page);
  if (!membership.teamId || !membership.renewalDate) throw new Error("Membership details are incomplete");
  run.checkpoints = {
    ...(run.checkpoints || {}),
    developerAccount: {
      state: "verified",
      verifiedAt: new Date().toISOString(),
      membershipHash: sha256(membership),
    },
  };
  await writeSecureJson(runPath(run), run);
  return { page, agreementAttemptId, membership };
}

// Stage 2: direct App ID registration and exact post-Register recovery.
async function exactBundleMatches(page, bundleId) {
  return await visibleInFrames(page, (frame) => frame.getByText(bundleId, { exact: true }));
}

async function exactIdentifierNameMatches(page, appName) {
  return await visibleInFrames(page, (frame) => frame.getByText(appName, { exact: true }));
}

async function identifierListIsStableEmpty(page) {
  let url;
  try { url = new URL(page.url()); } catch { return false; }
  if (url.hostname !== "developer.apple.com"
    || !url.pathname.startsWith("/account/resources/identifiers/list")) return false;
  const bodyText = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  return /Getting Started with App IDs/i.test(bodyText)
    && /Register an App ID/i.test(bodyText);
}

async function filterListByExactValue(page, value, label) {
  if (await identifierListIsStableEmpty(page)) return;
  const selector = "main input[type='search'], input[placeholder*='Search' i], input[type='search']";
  const deadline = Date.now() + TIMEOUT;
  let controls = [];
  do {
    if (await identifierListIsStableEmpty(page)) return;
    controls = [];
    for (const frame of page.frames()) {
      const candidates = frame.locator(selector);
      for (let index = 0; index < await candidates.count(); index += 1) {
        const candidate = candidates.nth(index);
        if (!await candidate.isVisible().catch(() => false)) continue;
        if (!await candidate.isEditable().catch(() => false)) continue;
        if (!await candidate.isEnabled().catch(() => false)) continue;
        if (!await candidate.boundingBox().catch(() => null)) continue;
        controls.push(candidate);
      }
    }
    if (controls.length === 1) break;
    if (controls.length > 1) {
      throw new Error(`${label} search control must be unique; found ${controls.length}`);
    }
    await page.waitForTimeout(20);
  } while (Date.now() < deadline);
  if (controls.length !== 1) {
    throw new Error(`${label} search control must be uniquely editable; found ${controls.length}`);
  }
  const control = controls[0];
  await control.fill(value);
  if ((await control.inputValue()).trim() !== value) throw new Error(`${label} search readback failed`);
  await page.waitForLoadState("networkidle", { timeout: TIMEOUT }).catch(() => {});
  const busy = page.locator("[aria-busy='true'], [role='progressbar']");
  await busy.first().waitFor({ state: "hidden", timeout: TIMEOUT }).catch(() => {});
}

async function clickUniqueContinue(page, label) {
  const target = await exactlyOne(
    page,
    (frame) => frame.getByRole("button", { name: "Continue", exact: true }),
    `${label} Continue`,
    { enabled: true },
  );
  await target.click({ timeout: SHORT_TIMEOUT });
}

async function chooseRadioByLabel(page, name) {
  const label = await exactlyOne(
    page,
    (frame) => frame.getByText(name, { exact: true }),
    `${name} option`,
  );
  const radio = label.locator("xpath=ancestor::label[1]//input[@type='radio']");
  if (await radio.count() === 1) {
    await radio.check({ timeout: SHORT_TIMEOUT });
    return;
  }
  await label.click({ timeout: SHORT_TIMEOUT });
}

async function fillIdentifierForm(page, config) {
  const description = await waitForUniqueVisibleInFrames(
    page,
    (frame) => frame.getByLabel(/Description/i),
    "App ID Description",
  );
  await description.fill(config.description);
  const explicit = await visibleInFrames(page, (frame) => frame.getByText("Explicit", { exact: true }));
  if (explicit.length === 1) await chooseRadioByLabel(page, "Explicit");
  const bundle = await waitForUniqueVisibleInFrames(
    page,
    (frame) => frame.getByLabel(/Bundle ID/i).or(frame.locator("input[name*='identifier'], input[id*='identifier']")),
    "Bundle ID input",
  );
  await bundle.fill(config.bundleId);
  if ((await description.inputValue()).trim() !== config.description) throw new Error("App ID Description readback failed");
  if ((await bundle.inputValue()).trim() !== config.bundleId) throw new Error("Bundle ID readback failed");
}

async function recoverIdentifier(page, config, { reload = false } = {}) {
  if (comparableUrl(page.url()) !== comparableUrl(IDENTIFIERS_URL)) {
    await page.goto(IDENTIFIERS_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  } else if (reload) {
    await page.reload({ waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  await waitForSafePage(page, "developer.apple.com");
  if (reload) {
    await page.waitForLoadState("networkidle", { timeout: TIMEOUT }).catch(() => {});
  }
  await filterListByExactValue(page, config.bundleId, "App ID");
  await page.locator("[aria-busy='true'], [role='progressbar']").first()
    .waitFor({ state: "hidden", timeout: TIMEOUT }).catch(() => {});
  await page.getByText(config.bundleId, { exact: true }).first()
    .waitFor({ state: "visible", timeout: TIMEOUT }).catch(() => {});
  const nameMatches = await exactIdentifierNameMatches(page, config.appName);
  if (nameMatches.length > 1) throw new Error("multiple exact App ID name matches found");
  if (nameMatches.length === 1) return true;
  const matches = await exactBundleMatches(page, config.bundleId);
  if (matches.length > 1) throw new Error("multiple exact App ID matches found after Register");
  return matches.length === 1;
}

async function registerIdentifierFromForm(page, run, config) {
  if (await handleDeveloperAgreementIfPresent(page, run)) {
    if (!new URL(page.url()).pathname.startsWith("/account/resources/identifiers/bundleId/add/bundle")) {
      await page.goto(BUNDLE_ID_ADD_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    }
    await waitForSafePage(page, "developer.apple.com");
    return await registerIdentifierFromForm(page, run, config);
  }
  let existingLedger = await readJson(ledgerPath(run, "app-id-register"));
  if (existingLedger && existingLedger.state !== "planned") {
    if (await recoverIdentifier(page, config, { reload: true })) {
      if (existingLedger.state !== "submitted") await updateLedger(run, existingLedger, "submitted");
      return existingLedger.attemptId;
    }
    const mayRetryConfirmedAbsence = ["clicking", "unknown"].includes(existingLedger.state)
      && Number(existingLedger.confirmedAbsenceRetryCount || 0) === 0
      && await identifierListIsStableEmpty(page);
    if (!mayRetryConfirmedAbsence) {
      throw new Error(`App ID registration attempt is ${existingLedger.state}; refusing to click Register again`);
    }
    existingLedger = {
      ...existingLedger,
      state: "planned",
      confirmedAbsenceRetryCount: 1,
      recoveryEvidence: "stable-empty-identifiers-list-after-premature-navigation",
      updatedAt: new Date().toISOString(),
    };
    await writeSecureJson(ledgerPath(run, "app-id-register"), existingLedger);
    await page.goto(BUNDLE_ID_ADD_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    await waitForSafePage(page, "developer.apple.com");
  }
  let registerCandidates = await visibleInFrames(
    page,
    (frame) => frame.getByRole("button", { name: "Register", exact: true }),
  );
  if (!registerCandidates.length) {
    await fillIdentifierForm(page, config);
    await clickUniqueContinue(page, "App ID review");
    await page.getByRole("button", { name: "Register", exact: true })
      .waitFor({ state: "visible", timeout: TIMEOUT });
    registerCandidates = await visibleInFrames(
      page,
      (frame) => frame.getByRole("button", { name: "Register", exact: true }),
    );
  }
  if (registerCandidates.length !== 1) {
    throw new Error(`Register must be uniquely visible; found ${registerCandidates.length}`);
  }
  const reviewText = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  const inputValues = await page.locator("input").evaluateAll(
    (inputs) => inputs.map((input) => String(input.value || "").trim()).filter(Boolean),
  );
  for (const [label, value] of [["Bundle ID", config.bundleId], ["Description", config.description]]) {
    if (!reviewText.includes(value) && !inputValues.includes(value)) {
      throw new Error(`review ${label} readback failed`);
    }
  }
  if (await handleDeveloperAgreementIfPresent(page, run)) {
    if (!new URL(page.url()).pathname.startsWith("/account/resources/identifiers/bundleId/add/bundle")) {
      await page.goto(BUNDLE_ID_ADD_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    }
    await waitForSafePage(page, "developer.apple.com");
    return await registerIdentifierFromForm(page, run, config);
  }
  let ledger = await prepareLedger(run, "app-id-register", page, {
    description: config.description,
    bundleId: config.bundleId,
  });
  if (ledger.state !== "planned") throw new Error("App ID ledger changed before Register");
  const register = registerCandidates[0];
  if (!await register.isEnabled()) throw new Error("Register is disabled");
  await assertLiveSession(run);
  assertAppleUrl(page.url(), "developer.apple.com");
  ledger = await updateLedger(run, ledger, "clicking");
  try {
    await register.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(run, ledger, "unknown");
    throw new Error(`Register click result is unknown: ${error.message}`);
  }
  const reachedList = await page.waitForURL((url) => (
    url.hostname === "developer.apple.com"
      && url.pathname.startsWith("/account/resources/identifiers/list")
  ), { waitUntil: "domcontentloaded", timeout: REGISTER_TRANSITION_TIMEOUT })
    .then(() => true)
    .catch(() => false);
  if (!reachedList) {
    await updateLedger(run, ledger, "unknown");
    throw new Error("Register was clicked once but Apple did not return to the Identifiers list");
  }
  if (!await recoverIdentifier(page, config, { reload: true })) {
    await updateLedger(run, ledger, "unknown");
    throw new Error("Register was clicked once but the exact App ID was not found");
  }
  await updateLedger(run, ledger, "submitted");
  return ledger.attemptId;
}

async function ensureIdentifier(page, run, config) {
  if (await recoverIdentifier(page, config)) {
    const ledger = await recordExistingLedger(run, "app-id-register", page, {
      description: config.description,
      bundleId: config.bundleId,
    });
    return { attemptId: ledger.attemptId, existing: true };
  }
  if (!new URL(page.url()).pathname.startsWith("/account/resources/identifiers/bundleId/add/bundle")) {
    await page.goto(BUNDLE_ID_ADD_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
    await waitForSafePage(page, "developer.apple.com");
  }
  return { attemptId: await registerIdentifierFromForm(page, run, config), existing: false };
}

// Stage 3: App Store Connect Add Apps, Create, and recovery.
async function exactExistingAppLinks(page, config) {
  const recoveryApps = [];
  const recoveryHrefs = new Set();
  const recoveryCandidates = await page.locator("a[href]").evaluateAll(
    (links) => links.map((link) => ({
      text: String(link.textContent || "").trim(),
      href: String(link.href || link.getAttribute("href") || ""),
    })),
  );
  for (const candidate of recoveryCandidates) {
    if (candidate.text !== config.appName || !candidate.href) continue;
    let url;
    try { url = new URL(candidate.href, "https://appstoreconnect.apple.com"); } catch { continue; }
    const distributionPath = url.pathname.replace(/\/+$/, "");
    if (url.hostname !== "appstoreconnect.apple.com"
      || !/^\/apps\/\d+\/distribution$/.test(distributionPath)) continue;
    const target = page.locator(`a[href="${candidate.href.replace(/["\\]/g, "\\$&")}"]`);
    if (await target.count() !== 1) continue;
    recoveryApps.push({ name: target, container: target });
    recoveryHrefs.add(url.toString());
  }
  const recoveryLinks = await visibleInFrames(page, (frame) => frame.locator("a[href]"));
  for (const link of recoveryLinks) {
    const text = (await link.innerText().catch(() => "")).trim();
    if (text !== config.appName) continue;
    const href = await link.getAttribute("href");
    if (!href) continue;
    let url;
    try { url = new URL(href, "https://appstoreconnect.apple.com"); } catch { continue; }
    const distributionPath = url.pathname.replace(/\/+$/, "");
    if (url.hostname !== "appstoreconnect.apple.com"
      || !/^\/apps\/\d+\/distribution$/.test(distributionPath)) continue;
    recoveryApps.push({ name: link, container: link });
    recoveryHrefs.add(url.toString());
  }
  const recoveryNames = await visibleInFrames(
    page,
    (frame) => frame.getByText(config.appName, { exact: true }),
  );
  for (const recoveryName of recoveryNames) {
    const recoveryLink = recoveryName.locator("xpath=ancestor-or-self::a[1]");
    if (await recoveryLink.count() !== 1) continue;
    const href = await recoveryLink.getAttribute("href");
    if (!href) continue;
    let url;
    try { url = new URL(href, "https://appstoreconnect.apple.com"); } catch { continue; }
    const distributionPath = url.pathname.replace(/\/+$/, "");
    if (url.hostname !== "appstoreconnect.apple.com"
      || !/^\/apps\/\d+\/distribution$/.test(distributionPath)) continue;
    if (!recoveryHrefs.has(url.toString())) {
      recoveryApps.push({ name: recoveryName, container: recoveryLink });
      recoveryHrefs.add(url.toString());
    }
  }
  if (recoveryApps.length > 1) {
    throw new Error(`multiple exact App Store app links found; count=${recoveryApps.length}`);
  }
  return recoveryApps;
}

async function appRows(page, config, { allowUniqueNameRecovery = false } = {}) {
  const openDialogs = await visibleInFrames(
    page,
    (frame) => frame.getByRole("dialog").getByText("New App", { exact: true }),
  );
  if (openDialogs.length > 1) throw new Error(`New App dialog must be unique; found ${openDialogs.length}`);
  if (openDialogs.length === 1) return [];
  let recoveryApps = await exactExistingAppLinks(page, config);
  if (recoveryApps.length === 1) return recoveryApps;
  await Promise.race([
    page.getByRole("button", { name: "Add Apps", exact: true }).waitFor({ state: "visible", timeout: TIMEOUT }),
    page.locator("main input[type='search'], main input[placeholder*='Search' i], input[type='search']")
      .first().waitFor({ state: "visible", timeout: TIMEOUT }),
  ]).catch(() => {});
  const emptyAddApps = await visibleInFrames(
    page,
    (frame) => frame.getByRole("button", { name: "Add Apps", exact: true }),
  );
  if (emptyAddApps.length > 1) throw new Error(`Add Apps must be uniquely visible; found ${emptyAddApps.length}`);
  if (emptyAddApps.length === 1) return [];
  recoveryApps = await exactExistingAppLinks(page, config);
  if (recoveryApps.length === 1) return recoveryApps;
  try {
    await filterListByExactValue(page, config.appName, "App Store app");
  } catch (error) {
    if (!allowUniqueNameRecovery) throw error;
    throw error;
  }
  const names = await visibleInFrames(page, (frame) => frame.getByText(config.appName, { exact: true }));
  const exact = [];
  const conflicting = [];
  for (const name of names) {
    const row = name.locator("xpath=ancestor::*[self::tr or @role='row' or self::li or self::a][1]");
    const container = await row.count() ? row : name.locator("xpath=ancestor::div[1]");
    const text = (await container.innerText().catch(() => "")).replace(/\s+/g, " ").trim();
    if (text.includes(config.bundleId)) exact.push({ name, container });
    else conflicting.push({ name, container });
  }
  if (exact.length > 1) throw new Error("multiple App Store apps match app name and Bundle ID");
  if (allowUniqueNameRecovery && exact.length === 0 && conflicting.length === 1) {
    return conflicting;
  }
  if (conflicting.length) throw new Error("App Store contains the same name with a different Bundle ID");
  return exact;
}

async function openExistingApp(page, config, options = {}) {
  const matches = await appRows(page, config, options);
  if (!matches.length) return false;
  if (await matches[0].container.evaluate((element) => element.matches("a[href]"))) {
    await matches[0].container.click({ timeout: SHORT_TIMEOUT });
  } else {
    const target = matches[0].container.getByRole("link", { name: config.appName, exact: true });
    if (await target.count() === 1) await target.click({ timeout: SHORT_TIMEOUT });
    else await matches[0].name.click({ timeout: SHORT_TIMEOUT });
  }
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  await waitForSafePage(page, "appstoreconnect.apple.com");
  return true;
}

async function openNewAppDialog(page) {
  let dialogs = await visibleInFrames(
    page,
    (frame) => frame.getByRole("dialog").getByText("New App", { exact: true }),
  );
  if (dialogs.length > 1) throw new Error(`New App dialog must be uniquely visible; found ${dialogs.length}`);
  if (dialogs.length === 1) return;
  let buttons = await visibleInFrames(
    page,
    (frame) => frame.getByRole("button", { name: "Add Apps", exact: true }),
  );
  if (buttons.length > 1) throw new Error(`Add Apps must be uniquely visible; found ${buttons.length}`);
  if (!buttons.length) {
    buttons = [
      ...await visibleInFrames(page, (frame) => frame.getByRole("button", { name: "New App", exact: true })),
      ...await visibleInFrames(page, (frame) => frame.getByRole("button", { name: /^(Add|Create|\+)$/i })),
    ];
  }
  const unique = [...new Set(buttons)];
  if (unique.length !== 1) throw new Error(`New App control must be uniquely visible; found ${unique.length}`);
  if (!await unique[0].isEnabled()) throw new Error("New App control is disabled");
  await unique[0].click({ timeout: SHORT_TIMEOUT });
  dialogs = await visibleInFrames(
    page,
    (frame) => frame.getByRole("dialog").getByText("New App", { exact: true }),
  );
  if (!dialogs.length) {
    const menuItem = await exactlyOne(
      page,
      (frame) => frame.getByRole("menuitem", { name: "New App", exact: true })
        .or(frame.getByRole("button", { name: "New App", exact: true }))
        .or(frame.getByText("New App", { exact: true })),
      "New App menu item",
      { enabled: true },
    );
    await menuItem.click({ timeout: SHORT_TIMEOUT });
    dialogs = await visibleInFrames(
      page,
      (frame) => frame.getByRole("dialog").getByText("New App", { exact: true }),
    );
  }
  if (dialogs.length !== 1) throw new Error(`New App dialog must be uniquely visible; found ${dialogs.length}`);
}

async function fillSelectOrPopup(page, label, optionText) {
  const control = await exactlyOne(page, (frame) => frame.getByLabel(label), `${label} control`);
  const tag = await control.evaluate((element) => element.tagName.toLowerCase());
  if (tag === "select") {
    if (label !== "Bundle ID") {
      await control.selectOption({ label: optionText });
    } else {
      await control.waitFor({ state: "visible", timeout: TIMEOUT });
      const enabledDeadline = Date.now() + TIMEOUT;
      while (!await control.isEnabled() && Date.now() < enabledDeadline) {
        await page.waitForTimeout(20);
      }
      if (!await control.isEnabled()) throw new Error("Bundle ID control stayed disabled");
      await control.click({ timeout: SHORT_TIMEOUT });
      const deadline = Date.now() + TIMEOUT;
      let matches = [];
      while (Date.now() < deadline) {
        matches = (await control.locator("option").evaluateAll((options) => options.map((option) => ({
          label: String(option.label || option.textContent || "").trim(),
          value: String(option.value || ""),
        })))).filter((option) => option.label.includes(optionText));
        if (matches.length) break;
        await page.waitForTimeout(20);
      }
      if (matches.length !== 1) {
        throw new Error(`Bundle ID option must uniquely contain the exact identifier; found ${matches.length}`);
      }
      await control.selectOption(matches[0].value);
    }
  } else {
    await control.click({ timeout: SHORT_TIMEOUT });
    const option = await exactlyOne(
      page,
      (frame) => frame.getByRole("option", { name: optionText, exact: true })
        .or(frame.getByText(optionText, { exact: true })),
      `${label} option`,
    );
    await option.click({ timeout: SHORT_TIMEOUT });
  }
}

async function fillNewApp(page, config) {
  const platform = await visibleInFrames(page, (frame) => frame.getByLabel(config.platform, { exact: true }));
  if (platform.length !== 1) throw new Error("iOS platform control must be uniquely visible");
  if (await platform[0].getAttribute("type") === "checkbox") await platform[0].check({ timeout: SHORT_TIMEOUT });
  else await platform[0].click({ timeout: SHORT_TIMEOUT });

  const name = await exactlyOne(page, (frame) => frame.getByLabel("Name", { exact: true }), "App Name");
  const sku = await exactlyOne(page, (frame) => frame.getByLabel("SKU", { exact: true }), "SKU");
  await name.fill(config.appName);
  await fillSelectOrPopup(page, "Primary Language", config.primaryLanguage);
  await fillSelectOrPopup(page, "Bundle ID", config.bundleId);
  await sku.fill(config.sku);
  await chooseRadioByLabel(page, config.accessLevel);

  if ((await name.inputValue()).trim() !== config.appName) throw new Error("App Name readback failed");
  if ((await sku.inputValue()).trim() !== config.sku) throw new Error("SKU readback failed");
  const body = (await page.locator("body").innerText()).replace(/\s+/g, " ");
  for (const expected of [config.primaryLanguage, config.bundleId, config.accessLevel]) {
    if (!body.includes(expected)) throw new Error("New App form readback failed");
  }
}

async function verifyAppDetail(page, _config) {
  await waitForSafePage(page, "appstoreconnect.apple.com");
  let body = "";
  for (const frame of page.frames()) {
    body += ` ${await frame.locator("body").innerText().catch(() => "")}`;
  }
  body = body.replace(/\s+/g, " ").trim();
  const versionPresent = /iOS(?: App)?(?: Version)?\s+1\.0\b/.test(body);
  const url = new URL(page.url());
  const detailPath = /^\/apps\/\d+\/distribution(?:\/|$)/.test(url.pathname);
  return detailPath && versionPresent;
}

async function waitForVerifiedAppDetail(page, config) {
  let initialUrl;
  try { initialUrl = new URL(page.url()); } catch { return false; }
  if (initialUrl.hostname !== "appstoreconnect.apple.com") return false;
  const deadline = Date.now() + APP_DETAIL_TIMEOUT;
  while (Date.now() < deadline) {
    if (await verifyAppDetail(page, config)) return true;
    await page.waitForTimeout(20);
  }
  return await verifyAppDetail(page, config);
}

async function recoverApp(page, config) {
  await page.goto(APPS_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await waitForSafePage(page, "appstoreconnect.apple.com");
  if (!await openExistingApp(page, config, { allowUniqueNameRecovery: true })) return false;
  return await waitForVerifiedAppDetail(page, config);
}

async function appStoreNoApps(page) {
  let url;
  try { url = new URL(page.url()); } catch { return false; }
  if (url.hostname !== "appstoreconnect.apple.com"
    || !(url.pathname === "/apps" || url.pathname === "/")) return false;
  const [noApps, addApps] = await Promise.all([
    visibleInFrames(page, (frame) => frame.getByText("No Apps", { exact: true })),
    visibleInFrames(page, (frame) => frame.getByRole("button", { name: "Add Apps", exact: true })),
  ]);
  if (noApps.length > 1) throw new Error(`No Apps marker must be uniquely visible; found ${noApps.length}`);
  if (addApps.length > 1) throw new Error(`Add Apps must be uniquely visible; found ${addApps.length}`);
  return noApps.length === 1 && addApps.length === 1;
}

async function appNameAlreadyUsedError(page) {
  const messages = await visibleInFrames(
    page,
    (frame) => frame.getByText(/The app name you entered is already being used/i),
  );
  const texts = [];
  for (const message of messages) {
    const text = (await message.innerText().catch(() => "")).replace(/\s+/g, " ").trim();
    if (text && !texts.includes(text)) texts.push(text);
  }
  texts.sort((left, right) => right.length - left.length);
  return texts[0] || null;
}

async function waitForCreateTransition(page, create) {
  const deadline = Date.now() + CREATE_TRANSITION_TIMEOUT;
  while (Date.now() < deadline) {
    const appNameError = await appNameAlreadyUsedError(page);
    if (appNameError) return appNameError;
    await page.waitForTimeout(20);
  }
  return await appNameAlreadyUsedError(page);
}

function appCreateInputSummary(config) {
  return {
    platform: config.platform,
    appName: config.appName,
    primaryLanguage: config.primaryLanguage,
    bundleId: config.bundleId,
    sku: config.sku,
    accessLevel: config.accessLevel,
  };
}

async function ensureApp(page, run, config) {
  if (await handleAppStoreAgreementIfPresent(page, run)) {
    return await ensureApp(page, run, config);
  }
  let existingLedger = await readJson(ledgerPath(run, "app-create"));
  if (existingLedger && ["clicking", "unknown"].includes(existingLedger.state)
    && await waitForVerifiedAppDetail(page, config)) {
    await updateLedger(run, existingLedger, "submitted");
    return { attemptId: existingLedger.attemptId, existing: true };
  }
  if (await waitForVerifiedAppDetail(page, config)) {
    const ledger = existingLedger || await recordExistingLedger(run, "app-create", page, {
      platform: config.platform,
      appName: config.appName,
      primaryLanguage: config.primaryLanguage,
      bundleId: config.bundleId,
      sku: config.sku,
      accessLevel: config.accessLevel,
    });
    if (ledger.state !== "submitted") await updateLedger(run, ledger, "submitted");
    return { attemptId: ledger.attemptId, existing: true };
  }
  if (await openExistingApp(page, config, { allowUniqueNameRecovery: true })) {
    if (!await waitForVerifiedAppDetail(page, config)) throw new Error("existing App Store app detail markers are incomplete");
    const ledger = existingLedger || await recordExistingLedger(run, "app-create", page, {
      platform: config.platform,
      appName: config.appName,
      primaryLanguage: config.primaryLanguage,
      bundleId: config.bundleId,
      sku: config.sku,
      accessLevel: config.accessLevel,
    });
    if (ledger.state !== "submitted") await updateLedger(run, ledger, "submitted");
    return { attemptId: ledger.attemptId, existing: true };
  }
  if (existingLedger && existingLedger.state !== "planned") {
    if (await appStoreNoApps(page)) {
      const details = {
        kind: "app-create",
        sessionId: run.sessionId,
        pageIdentity: { url: safeUrl(page.url()), titleHash: sha256(await page.title()) },
        inputSummaryHash: sha256(appCreateInputSummary(config)),
      };
      existingLedger = {
        attemptId: `app-create-${sha256(details).slice(0, 24)}`,
        ...details,
        state: "planned",
        recoveryEvidence: "verified-no-apps",
        previousAttemptId: existingLedger.attemptId,
        createdAt: new Date().toISOString(),
        recoveredAt: new Date().toISOString(),
      };
      await writeSecureJson(ledgerPath(run, "app-create"), existingLedger);
    } else {
      throw new Error(`App creation attempt is ${existingLedger.state}; read-only recovery found no exact app`);
    }
  }

  await openNewAppDialog(page);
  await fillNewApp(page, config);
  if (await handleAppStoreAgreementIfPresent(page, run)) {
    return await ensureApp(page, run, config);
  }
  let ledger = await prepareLedger(run, "app-create", page, {
    platform: config.platform,
    appName: config.appName,
    primaryLanguage: config.primaryLanguage,
    bundleId: config.bundleId,
    sku: config.sku,
    accessLevel: config.accessLevel,
  });
  if (ledger.state !== "planned") throw new Error("App creation ledger changed before Create");
  let create = await exactlyOne(
    page,
    (frame) => frame.getByRole("dialog").getByRole("button", { name: "Create", exact: true }),
    "Create",
    { enabled: true },
  );
  await assertLiveSession(run);
  assertAppleUrl(page.url(), "appstoreconnect.apple.com");
  await page.waitForTimeout(2_000);
  if (!await create.isVisible().catch(() => false) || !await create.isEnabled().catch(() => false)) {
    throw new Error("New App Create button changed during the pre-click wait");
  }
  ledger = await updateLedger(run, ledger, "clicking");
  let appNameError = null;
  try {
    await create.click({ timeout: SHORT_TIMEOUT });
    appNameError = await waitForCreateTransition(page, create);
  } catch (error) {
    await updateLedger(run, ledger, "unknown");
    throw new Error(`Create click result is unknown: ${error.message}`);
  }
  if (appNameError) {
    ledger = {
      ...ledger,
      state: "rejected",
      rejectionEvidence: "app-name-already-used",
      rejectionMessage: appNameError,
      updatedAt: new Date().toISOString(),
    };
    await writeSecureJson(ledgerPath(run, "app-create"), ledger);
    throw new Error(`${appNameError}`);
  }
  await page.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});
  if (!await recoverApp(page, config)) {
    await updateLedger(run, ledger, "unknown");
    throw new Error("Create was clicked once but reopening Apps did not verify the exact app");
  }
  await updateLedger(run, ledger, "submitted");
  return { attemptId: ledger.attemptId, existing: false };
}

// Stage 4: final page, ledger, checkpoint, and file-permission verification.
async function verifyWorkflowCompletion(context, page, run, config) {
  await assertLiveSession(run);
  const pages = context.pages();
  if (pages.length !== 1 || pages[0] !== page || !isApprovedApplePage(pages[0])) {
    throw new Error(`completion requires one verified Apple browser tab; found ${pages.length}`);
  }
  if (!await waitForVerifiedAppDetail(page, config)) throw new Error("final App Store detail verification failed");
  const [registerLedger, createLedger] = await Promise.all([
    readJson(ledgerPath(run, "app-id-register")),
    readJson(ledgerPath(run, "app-create")),
  ]);
  if (registerLedger?.state !== "submitted") throw new Error("App ID Register ledger is not submitted");
  if (createLedger?.state !== "submitted") throw new Error("App Create ledger is not submitted");
  if (run.checkpoints?.developerAccount?.state !== "verified"
    || run.checkpoints?.appId?.state !== "verified") {
    throw new Error("required completion checkpoints are not verified");
  }
  const url = new URL(page.url());
  const match = url.pathname.match(/^\/apps\/(\d+)\/distribution(?:\/|$)/);
  if (!match) throw new Error("numeric App ID is absent from the verified detail URL");
  return { numericAppId: match[1] };
}

async function verifyPersistedCompletion(run) {
  const [persistedRun, registerLedger, createLedger] = await Promise.all([
    readJson(runPath(run)),
    readJson(ledgerPath(run, "app-id-register")),
    readJson(ledgerPath(run, "app-create")),
  ]);
  const requiredCheckpoints = ["developerAccount", "appId", "appStoreAgreement", "appStoreApp"];
  if (persistedRun?.state !== "verified" || !persistedRun.completedAt) {
    throw new Error("persisted UTM-12 completion state is not verified");
  }
  for (const checkpoint of requiredCheckpoints) {
    if (persistedRun.checkpoints?.[checkpoint]?.state !== "verified") {
      throw new Error(`persisted checkpoint is not verified: ${checkpoint}`);
    }
  }
  if (registerLedger?.state !== "submitted" || createLedger?.state !== "submitted") {
    throw new Error("persisted irreversible-action ledgers are incomplete");
  }
  for (const file of [runPath(run), ledgerPath(run, "app-id-register"), ledgerPath(run, "app-create")]) {
    if (((await stat(file)).mode & 0o777) !== 0o600) {
      throw new Error(`completion secure file mode must be 600: ${path.basename(file)}`);
    }
  }
}

function resultFromPersistedRun(run) {
  const teamId = run.result?.teamId;
  const renewalDate = run.result?.renewalDate;
  const numericAppId = run.result?.numericAppId;
  if (!/^[A-Z0-9]{10}$/.test(String(teamId || ""))) return null;
  if (!String(renewalDate || "").trim()) return null;
  if (!/^\d+$/.test(String(numericAppId || ""))) return null;
  const appsUrl = new URL(String(run.pages?.apps || ""));
  const urlAppId = appsUrl.pathname.match(/^\/apps\/(\d+)\/distribution(?:\/|$)/)?.[1];
  if (appsUrl.hostname !== "appstoreconnect.apple.com" || urlAppId !== numericAppId) return null;
  return {
    teamId,
    renewalDate: renewalDate.trim(),
    numericAppId,
  };
}

async function replayVerifiedUtm12(context, run) {
  const result = resultFromPersistedRun(run);
  if (run.state !== "verified" || !result) return null;
  const targetUrl = String(run.pages?.apps || "");
  let target;
  try { target = new URL(targetUrl); } catch { return null; }
  if (target.hostname !== "appstoreconnect.apple.com"
    || !/^\/apps\/\d+\/distribution(?:\/|$)/.test(target.pathname)) return null;
  const page = context.pages().findLast(isApprovedApplePage);
  if (!page) return null;
  await verifyBeforeNavigation(page);
  if (comparableUrl(page.url()) !== comparableUrl(targetUrl)) {
    await page.goto(targetUrl, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  if (!await waitForVerifiedAppDetail(page, null)) return null;
  return {
    LOCAL_BROWSER_SESSION: "verified",
    LOCAL_BROWSER_SESSION_ID: run.sessionId,
    DEVELOPER_ACCOUNT: "opened",
    AGREEMENT_ATTEMPT_ID: "existing",
    AGREEMENT_ACCEPTED: "verified",
    APP_ID_REGISTER_ATTEMPT_ID: "existing",
    APP_ID_REGISTERED: "verified",
    APP_STORE_AGREEMENT_ATTEMPT_ID: "existing",
    APP_STORE_AGREEMENT_ACCEPTED: "verified",
    APP_CREATE_ATTEMPT_ID: "existing",
    APP_STORE_APP: "created_or_existing_exact",
    EXISTING_IDENTIFIER: true,
    EXISTING_APP: true,
    NUMERIC_APP_ID: result.numericAppId,
    WORKFLOW_PAGE: "single_verified",
    team_id: result.teamId,
    renewal_date: result.renewalDate,
    UTM_12: "verified",
  };
}

export async function recoverUtm12NotionFields() {
  const { context, run } = await connectExistingUtm11Session();
  await verifyPersistedCompletion(run);
  let result = resultFromPersistedRun(run);
  if (!result) {
    const appsUrl = new URL(String(run.pages?.apps || ""));
    const numericAppId = appsUrl.pathname.match(/^\/apps\/(\d+)\/distribution(?:\/|$)/)?.[1];
    if (appsUrl.hostname !== "appstoreconnect.apple.com" || !numericAppId) {
      throw new Error("persisted App Store app URL is invalid");
    }
    const pages = context.pages().filter(isApprovedApplePage);
    if (pages.length !== 1) throw new Error(`notion field recovery requires one Apple tab; found ${pages.length}`);
    const page = pages[0];
    const account = await openPage(
      context,
      run,
      "developerAccount",
      DEVELOPER_ACCOUNT_URL,
      "developer.apple.com",
      page,
      { acceptedPathPrefix: "/account/agree/" },
    );
    const membership = await readMembership(account);
    const app = await openPage(
      context,
      run,
      "apps",
      appsUrl.toString(),
      "appstoreconnect.apple.com",
      account,
      { acceptedPathPrefix: appsUrl.pathname },
    );
    if (!await waitForVerifiedAppDetail(app, null)) {
      throw new Error("persisted App Store app detail is not verified");
    }
    run.result = {
      teamId: membership.teamId,
      renewalDate: membership.renewalDate,
      numericAppId,
    };
    await writeSecureJson(runPath(run), run);
    result = resultFromPersistedRun(run);
  }
  if (!result) throw new Error("persisted UTM-12 result is invalid");
  return {
    LOCAL_BROWSER_SESSION: "verified",
    LOCAL_BROWSER_SESSION_ID: run.sessionId,
    MEMBERSHIP_DETAILS: "verified",
    NUMERIC_APP_ID: result.numericAppId,
    team_id: result.teamId,
    renewal_date: result.renewalDate,
    UTM_12: "verified",
  };
}

export async function runUtm12({ adoptCurrentSession = false } = {}) {
  // 1. Preflight: reuse the verified UTM-11 session and require one Apple tab.
  const { context, run } = await connectExistingUtm11Session({ adoptCurrentSession });
  const replay = await replayVerifiedUtm12(context, run);
  if (replay) return replay;
  const approvedPages = context.pages().filter(isApprovedApplePage);
  const keepPage = approvedPages.find((page) => {
    try { return new URL(page.url()).pathname.startsWith("/account"); } catch { return false; }
  }) || approvedPages.at(-1);
  if (!keepPage) throw new Error("the inherited Apple browser tab is unavailable");
  const keepPageUrl = new URL(keepPage.url());
  let inheritedAppDetailUrl = null;
  if (keepPageUrl.hostname === "appstoreconnect.apple.com") {
    inheritedAppDetailUrl = await waitForVerifiedAppDetail(keepPage, null)
      ? keepPage.url()
      : null;
  }
  const inheritedAppDetailPath = inheritedAppDetailUrl
    ? new URL(inheritedAppDetailUrl).pathname
    : null;
  const persistedResult = resultFromPersistedRun(run);
  let persistedAppDetailUrl = null;
  if (persistedResult) {
    try {
      const candidate = new URL(String(run.pages?.apps || ""));
      if (candidate.hostname === "appstoreconnect.apple.com"
        && /^\/apps\/\d+\/distribution(?:\/|$)/.test(candidate.pathname)) {
        persistedAppDetailUrl = candidate.toString();
      }
    } catch {
      persistedAppDetailUrl = null;
    }
  }
  for (const page of context.pages()) {
    if (page !== keepPage) await page.close().catch(() => {});
  }

  // 2. Developer Account: handle the Developer agreement, then read membership.
  const {
    page: account,
    agreementAttemptId,
    membership,
  } = await verifyDeveloperAccountStage(context, run);
  if (run.checkpoints.developerAccount.state !== "verified") {
    throw new Error("Developer Account checkpoint is not verified");
  }

  // 3. Input: resolve app name and exact Bundle ID only after Account succeeds.
  const config = await loadAppConfig();
  if (!config.bundleId) throw new Error("App ID registration requires the exact Bundle ID");
  config.sku ||= config.bundleId;
  if (config.resetFromIdentifiers) await resetFromIdentifiers(run, config);

  let workflowPage = account;
  let appIdAttemptId = (await readJson(ledgerPath(run, "app-id-register")))?.attemptId || "existing";

  // 4. App ID: verify an existing exact Bundle ID first; register only when absent.
  if (run.checkpoints?.appId?.state !== "verified") {
    workflowPage = await openPage(
      context,
      run,
      "identifiers",
      IDENTIFIERS_URL,
      "developer.apple.com",
      account,
      { acceptedPathPrefix: "/account/resources/identifiers" },
    );
    const identifierResult = await ensureIdentifier(workflowPage, run, config);
    appIdAttemptId = identifierResult.attemptId;
    run.checkpoints = {
      ...(run.checkpoints || {}),
      appId: { state: "verified", verifiedAt: new Date().toISOString() },
      appIdExisting: identifierResult.existing,
    };
    await writeSecureJson(runPath(run), run);
  }
  if (run.checkpoints.appId.state !== "verified") throw new Error("App ID checkpoint is not verified");

  // 5. App Store Connect: intercept Terms Of Service, then Add Apps/Create once.
  const appsTargetUrl = inheritedAppDetailUrl || persistedAppDetailUrl || APPS_URL;
  const apps = await openPage(
    context,
    run,
    "apps",
    appsTargetUrl,
    "appstoreconnect.apple.com",
    workflowPage,
    { acceptedPathPrefix: inheritedAppDetailPath },
  );
  const appStoreAgreementAttemptId = await handleAppStoreAgreementIfPresent(apps, run)
    || (await readJson(ledgerPath(run, "app-store-connect-agreement")))?.attemptId
    || "existing";
  if (!await appStoreAgreementComplete(apps)) throw new Error("App Store Connect Apps page is not verified");
  run.checkpoints = {
    ...(run.checkpoints || {}),
    appStoreAgreement: { state: "verified", verifiedAt: new Date().toISOString() },
  };
  await writeSecureJson(runPath(run), run);
  const appResult = await ensureApp(apps, run, config);
  const appCreateAttemptId = appResult.attemptId;

  // 6. Completion: verify numeric App ID, page markers, ledgers, checkpoints, and modes.
  const completion = await verifyWorkflowCompletion(context, apps, run, config);

  run.state = "verified";
  run.completedAt = new Date().toISOString();
  run.checkpoints = {
    ...(run.checkpoints || {}),
    appStoreApp: { state: "verified", verifiedAt: run.completedAt },
  };
  run.result = {
    teamId: membership.teamId,
    renewalDate: membership.renewalDate,
    numericAppId: completion.numericAppId,
  };
  await writeSecureJson(runPath(run), run);
  await verifyPersistedCompletion(run);
  return {
    LOCAL_BROWSER_SESSION: "verified",
    LOCAL_BROWSER_SESSION_ID: run.sessionId,
    DEVELOPER_ACCOUNT: "opened",
    AGREEMENT_ATTEMPT_ID: agreementAttemptId,
    AGREEMENT_ACCEPTED: "verified",
    APP_ID_REGISTER_ATTEMPT_ID: appIdAttemptId,
    APP_ID_REGISTERED: "verified",
    APP_STORE_AGREEMENT_ATTEMPT_ID: appStoreAgreementAttemptId,
    APP_STORE_AGREEMENT_ACCEPTED: "verified",
    APP_CREATE_ATTEMPT_ID: appCreateAttemptId,
    APP_STORE_APP: "created_or_existing_exact",
    EXISTING_IDENTIFIER: Boolean(run.checkpoints.appIdExisting || appIdAttemptId === "existing"),
    EXISTING_APP: appResult.existing,
    NUMERIC_APP_ID: completion.numericAppId,
    WORKFLOW_PAGE: "single_verified",
    team_id: membership.teamId,
    renewal_date: membership.renewalDate,
    UTM_12: "verified",
  };
}

async function main() {
  const [command, ...extra] = process.argv.slice(2);
  if (extra.length || (command && !["notion-fields", "adopt-current-session"].includes(command))) {
    throw new Error("Usage: node scripts/utm_12_one.mjs [notion-fields|adopt-current-session]");
  }
  const result = command === "notion-fields"
    ? await recoverUtm12NotionFields()
    : await runUtm12({ adoptCurrentSession: command === "adopt-current-session" });
  process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
}

if (fileURLToPath(import.meta.url) === path.resolve(process.argv[1] || "")) {
  main().catch((error) => {
    process.stderr.write(`UTM_12_ERROR=${error.message}\n`, () => process.exit(1));
  });
}
