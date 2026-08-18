#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const TIMEOUT = 30_000;
const SHORT_TIMEOUT = 5_000;
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const RUNTIME_ROOT = path.join(SCRIPT_DIR, ".utm-business-state");
const CDP_PORT = Number(process.env.LOCAL_BROWSER_CDP_PORT || "9222");
const BUSINESS_URL = "https://appstoreconnect.apple.com/business";
const BANK_PROCESSING_TEXT = "Your banking updates are processing, and you should see the changes in 24 hours. You won't be able to make any additional updates until then.";
let otpInput = null;

function emitProgress(message) {
  process.stderr.write(`UTM_BUSINESS_PROGRESS=${message.replace(/[\r\n]+/g, " ")}\n`);
}

function emitStepResult(label, status) {
  if (status === "existing") emitProgress(`${label}检查已存在，跳过`);
  else if (status === "completed") emitProgress(`${label}已完成`);
  else throw new Error(`${label} returned an invalid step status`);
}

async function runReportedStep(label, operation) {
  emitProgress(`开始 ${label}`);
  const result = await operation();
  emitStepResult(label, typeof result === "string" ? result : result?.status);
  return result;
}

function reportExistingStep(label) {
  emitProgress(`开始 ${label}`);
  emitStepResult(label, "existing");
}

async function loadSessionModule() {
  return await import(new URL("./session.mjs", import.meta.url).href);
}

function hash(value) {
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value));
  return createHash("sha256").update(bytes).digest("hex");
}

function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function assertApplePage(page) {
  const url = new URL(page.url());
  const hosts = ["apple.com", "developer.apple.com", "appstoreconnect.apple.com", "idmsa.apple.com"];
  if (url.protocol !== "https:" || !hosts.some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`))) {
    throw new Error(`unexpected browser page: ${safeUrl(page.url())}`);
  }
}

async function readJson(file) {
  try { return JSON.parse(await readFile(file, "utf8")); }
  catch (error) { if (error?.code === "ENOENT") return null; throw error; }
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

function sessionDir(contextId) {
  if (!/^[A-Za-z0-9-]{8,80}$/.test(contextId)) throw new Error("invalid utm-business context ID");
  return path.join(RUNTIME_ROOT, contextId);
}

function runPath(contextId) { return path.join(sessionDir(contextId), "run.json"); }
function ledgerPath(contextId, kind) { return path.join(sessionDir(contextId), `${kind}-attempt.json`); }
function bankEntityPath(contextId) { return path.join(sessionDir(contextId), "bank-account-legal-entity.json"); }

async function browserVersion() {
  try {
    const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, {
      signal: AbortSignal.timeout(SHORT_TIMEOUT),
    });
    return response.ok ? await response.json() : null;
  } catch { return null; }
}

function liveSessionId(info) {
  return String(info?.webSocketDebuggerUrl || "").split("/").pop();
}

function workPageName(contextId) {
  if (!/^[A-Za-z0-9-]{8,80}$/.test(contextId)) throw new Error("invalid utm-business context ID");
  return `codex-utm-business-${contextId}`;
}

async function pageName(page) {
  return await page.evaluate(() => window.name).catch(() => "");
}

async function pageTargetId(page) {
  const cdp = await page.context().newCDPSession(page);
  try {
    const result = await cdp.send("Target.getTargetInfo");
    const targetId = result?.targetInfo?.targetId;
    if (!/^[a-f0-9-]{8,}$/i.test(targetId || "")) throw new Error("invalid browser target ID");
    return targetId;
  } finally { await cdp.detach().catch(() => {}); }
}

async function findBoundWorkPage(context, expectedName, expectedTargetId = null) {
  const matches = [];
  for (const candidate of context.pages()) {
    if (expectedTargetId) {
      if (await pageTargetId(candidate).catch(() => null) === expectedTargetId) matches.push(candidate);
    } else if (await pageName(candidate) === expectedName) matches.push(candidate);
  }
  if (matches.length > 1) throw new Error(`utm-business work page binding is duplicated; found ${matches.length}`);
  return matches[0] || null;
}

async function assertBoundWorkPage(page, expectedName, expectedTargetId = null) {
  if (page.isClosed()) throw new Error("utm-business work page was closed");
  if (await pageName(page) !== expectedName) throw new Error("current page is not the utm-business bound work page");
  if (expectedTargetId && await pageTargetId(page) !== expectedTargetId) throw new Error("utm-business work page target ID changed");
}

async function connectSession(input) {
  const { connectLocalBrowser, currentSessionIdentity } = await loadSessionModule();
  const identity = await currentSessionIdentity();
  const connected = await connectLocalBrowser();
  const browserSessionId = liveSessionId({ webSocketDebuggerUrl: identity.edge_websocket });
  if (!browserSessionId || connected.sessionId !== browserSessionId) {
    throw new Error("the existing Edge session identity is invalid");
  }
  if (identity.edge_pid !== input.expectedEdgePid ||
      identity.edge_websocket !== input.expectedEdgeWebsocket) {
    throw new Error("the existing Edge session does not match the host handoff");
  }
  const contextId = input.contextId;
  const existing = await readJson(runPath(contextId));
  const inputSummaryHash = hash({
    schemaVersion: 4,
    contextId,
    browserSessionId,
    vmName: input.vmName,
    appNameHash: hash(input.appName),
    bundleIdHash: hash(input.bundleId),
    decisions: {
      dsa: "not-trader-no-eu-distribution", paidAppsAgreement: "accept",
      taxQuestionnaire: ["resident-no", "business-activities-no"], title: "CEO", dac7: "No",
    },
    birthdayHash: hash(input.birthday.iso),
  });
  const run = existing || {
    runId: randomUUID(), contextId, browserSessionId, edgePid: identity.edge_pid,
    edgeWebsocket: identity.edge_websocket, schemaVersion: 4,
    inputSummaryHash, state: "started", createdAt: new Date().toISOString(), checkpoints: {}, pages: {},
  };
  if (run.contextId !== contextId || run.browserSessionId !== browserSessionId ||
      run.edgePid !== identity.edge_pid || run.edgeWebsocket !== identity.edge_websocket) {
    throw new Error("existing utm-business state belongs to another Edge session");
  }
  if (run.inputSummaryHash && run.inputSummaryHash !== inputSummaryHash) {
    throw new Error("existing utm-business state conflicts with the current inputs");
  }
  run.schemaVersion = 4;
  run.inputSummaryHash = inputSummaryHash;
  run.checkpoints ||= {};
  run.pages ||= {};
  const expectedName = run.workPageName || workPageName(contextId);
  if (run.workPageName && run.workPageName !== expectedName) throw new Error("utm-business work page binding changed");
  run.workPageName = expectedName;
  run.workPage ||= { state: "planned", nameHash: hash(expectedName), plannedAt: new Date().toISOString() };
  await writeSecureJson(runPath(contextId), run);

  let expectedTargetId = run.workPage.targetId || null;
  let page = await findBoundWorkPage(connected.context, expectedName, expectedTargetId);
  if (existing && existing.state !== "verified") {
    if (page) await page.close();
    run.workPage = {
      state: "creating",
      nameHash: hash(expectedName),
      reopenReason: "full-entry-replay",
      creatingAt: new Date().toISOString(),
    };
    run.pages.work = {
      state: "creating",
      nameHash: hash(expectedName),
      reopenReason: "full-entry-replay",
    };
    run.state = "started";
    await writeSecureJson(runPath(contextId), run);
    expectedTargetId = null;
    page = null;
  }
  if (!page) {
    if (existing && existing.state === "verified" && (["creating", "created", "bound"].includes(run.workPage.state) ||
        Object.keys(run.checkpoints).length || run.state === "verified")) {
      throw new Error("the bound utm-business work page is missing; refusing to open a replacement page");
    }
    run.workPage = {
      ...run.workPage, state: "creating", nameHash: hash(expectedName), creatingAt: new Date().toISOString(),
    };
    await writeSecureJson(runPath(contextId), run);
    page = await connected.context.newPage();
    await page.evaluate((name) => { window.name = name; }, expectedName);
    expectedTargetId = await pageTargetId(page);
    await assertBoundWorkPage(page, expectedName, expectedTargetId);
    run.workPage = {
      state: "created", targetId: expectedTargetId, targetIdHash: hash(expectedTargetId),
      nameHash: hash(expectedName), createdAt: new Date().toISOString(),
      initialUrl: "about:blank",
    };
    run.pages.work = { state: "created", targetIdHash: hash(expectedTargetId), nameHash: hash(expectedName) };
    await writeSecureJson(runPath(contextId), run);
    await page.goto(BUSINESS_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  } else if (!expectedTargetId) {
    expectedTargetId = await pageTargetId(page);
    run.workPage = { ...run.workPage, targetId: expectedTargetId, targetIdHash: hash(expectedTargetId) };
    run.pages.work = { ...(run.pages.work || {}), targetIdHash: hash(expectedTargetId), nameHash: hash(expectedName) };
    await writeSecureJson(runPath(contextId), run);
  }
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  assertApplePage(page);
  return { page, contextId, browserSessionId, run, expectedName, expectedTargetId };
}

async function visible(locator) {
  const result = [];
  for (let i = 0; i < await locator.count(); i += 1) {
    const item = locator.nth(i);
    if (await item.isVisible().catch(() => false)) result.push(item);
  }
  return result;
}

async function unique(items, label, { enabled = false } = {}) {
  if (items.length !== 1) throw new Error(`${label} must be uniquely visible; found ${items.length}`);
  if (enabled && !await items[0].isEnabled().catch(() => false)) throw new Error(`${label} is disabled`);
  return items[0];
}

async function roleAction(page, name, options = {}) {
  const exact = options.exact ?? true;
  const items = [
    ...await visible(page.getByRole("button", { name, exact })),
    ...await visible(page.getByRole("link", { name, exact })),
  ];
  return unique(items, String(name), { enabled: options.enabled ?? true });
}

async function enabledRoleAction(scope, name, label) {
  const action = scope.getByRole("button", { name, exact: true });
  await action.waitFor({ state: "visible", timeout: TIMEOUT });
  await action.click({ trial: true, timeout: TIMEOUT });
  return unique(await visible(action), label, { enabled: true });
}

async function textContainer(page, text) {
  const marker = await unique(await visible(page.getByText(text, { exact: true })), String(text));
  return marker.locator("xpath=ancestor::*[self::tr or @role='row' or self::section or self::article or self::div][1]");
}

async function itemRowStatus(page, itemName) {
  const markers = await visible(page.getByText(itemName, { exact: true }));
  if (!markers.length) return null;
  const found = [];
  for (const marker of markers) {
    const status = await marker.evaluate((node) => {
      const statuses = ["Active", "New", "Processing", "Pending", "Expired"];
      for (let current = node; current && current !== document.body; current = current.parentElement) {
        if (current.tagName === "TR" || current.getAttribute("role") === "row") {
          const directTexts = [...current.children]
            .filter((element) => element.getClientRects().length > 0)
            .map((element) => String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim());
          const candidate = statuses.find((value) => directTexts.includes(value));
          return candidate || null;
        }
      }
      return null;
    });
    if (status) found.push(status);
  }
  const uniqueStatuses = [...new Set(found)];
  if (uniqueStatuses.length > 1) throw new Error(`${itemName} has conflicting visible Status values`);
  return uniqueStatuses[0] || null;
}

async function controlByText(page, text, type = "checkbox") {
  const byRole = await visible(page.getByRole(type, { name: text, exact: false }));
  if (byRole.length === 1) return byRole[0];
  if (byRole.length > 1) return unique(byRole, `${type} for ${text}`);
  const labelled = await visible(page.getByLabel(text, { exact: false }));
  if (labelled.length === 1) return labelled[0];
  const matches = [];
  for (const marker of await visible(page.getByText(text, { exact: false }))) {
    const label = marker.locator("xpath=ancestor-or-self::label[1]");
    if (await label.count()) {
      const controls = await visible(label.locator(`input[type='${type}']`));
      if (controls.length) { matches.push(...controls); continue; }
    }
    const container = marker.locator(`xpath=ancestor::*[.//input[@type='${type}']][1]`);
    const controls = container.locator(`input[type='${type}']`);
    if (await controls.count() === 1) matches.push(controls.first());
  }
  return unique(matches, `${type} for ${text}`);
}

async function setControl(page, text, type = "checkbox") {
  const control = await controlByText(page, text, type);
  if (!await control.isChecked()) {
    const tagName = await control.evaluate((element) => element.tagName);
    if (tagName === "INPUT") {
      if (await control.isVisible().catch(() => false)) await control.check({ timeout: SHORT_TIMEOUT });
      else await control.evaluate((element) => element.click());
    }
    else await control.evaluate((element) => element.click());
  }
  if (!await control.isChecked()) throw new Error(`${text} did not remain selected`);
  return control;
}

async function fieldByLabel(scope, label, fallbackSelector = null) {
  const labelled = await visible(scope.getByLabel(label, { exact: true }));
  if (labelled.length === 1) return labelled[0];
  if (labelled.length > 1) return unique(labelled, label);
  if (fallbackSelector) {
    const fallback = scope.locator(fallbackSelector);
    await fallback.waitFor({ state: "visible", timeout: TIMEOUT });
    return unique(await visible(fallback), label);
  }
  return unique([], label);
}

async function safePage(page) {
  assertApplePage(page);
  await page.locator("body").waitFor({ state: "visible", timeout: TIMEOUT });
  const body = await page.locator("body").innerText();
  if (/captcha|verify you are human|unusual activity|account locked/i.test(body)) {
    throw new Error("Apple security challenge detected");
  }
  if (await page.locator("input[type='password'], input#account_name_text_field").count()) {
    throw new Error("Apple login is required; rerun utm-10 first");
  }
}

async function bodyText(page) { return (await page.locator("body").innerText()).replace(/\s+/g, " ").trim(); }

async function visibleAcrossFrames(page, factory) {
  const found = [];
  for (const frame of page.frames()) {
    let locator;
    try { locator = factory(frame); } catch { continue; }
    for (let index = 0; index < await locator.count().catch(() => 0); index += 1) {
      const item = locator.nth(index);
      if (await item.isVisible().catch(() => false)) found.push(item);
    }
  }
  return found;
}

async function twoFactorPending(page) {
  const inputs = await visibleAcrossFrames(page, (frame) =>
    frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
  if (inputs.length > 0) return true;
  const phoneTail = String(otpInput?.phone || "").replace(/\D/g, "").slice(-2);
  if (phoneTail.length === 2) {
    const choices = await visibleAcrossFrames(page, (frame) => frame.getByRole("button"));
    for (const choice of choices) {
      const label = `${await choice.innerText()} ${await choice.getAttribute("aria-label") || ""}`;
      if (label.replace(/\D/g, "").endsWith(phoneTail)) return true;
    }
  }
  return /Two-Factor Authentication Required|verification code/i.test(await bodyText(page));
}

async function waitForStableBusinessPage(page) {
  await page.waitForLoadState("networkidle", { timeout: TIMEOUT }).catch(() => {});
  const deadline = Date.now() + TIMEOUT;
  let previous = null;
  let stableSamples = 0;
  while (Date.now() < deadline) {
    const headings = (await page.getByRole("heading").allTextContents()).map((text) => text.replace(/\s+/g, " ").trim());
    const actions = [
      ...await page.getByRole("button").allTextContents(),
      ...await page.getByRole("link").allTextContents(),
    ].map((text) => text.replace(/\s+/g, " ").trim()).filter(Boolean);
    const signature = hash({ headings, actions });
    const ready = headings.includes("Business") && (
      headings.includes("Agreements") || headings.includes("Tax Forms") || headings.includes("Compliance") ||
      headings.includes("U.S. Certificate of Foreign Status of Beneficial Owner") || headings.includes("U.S. Form W-8BEN") ||
      actions.includes("Complete Compliance Requirements")
    );
    if (ready && signature === previous) stableSamples += 1;
    else stableSamples = 0;
    if (stableSamples >= 2) return;
    previous = signature;
    await page.waitForTimeout(500);
  }
  throw new Error("Business page did not reach a stable Agreements state");
}

async function waitUntil(page, predicate, label, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try { if (await predicate()) return; } catch (error) { lastError = error; }
    await page.waitForTimeout(250);
  }
  throw new Error(`${label} was not verified${lastError ? `: ${lastError.message}` : ""}`);
}

async function checkpoint(run, sessionId, name, evidence = true) {
  run.checkpoints[name] = { state: "verified", evidenceHash: hash(evidence), at: new Date().toISOString() };
  await writeSecureJson(runPath(sessionId), run);
}

async function prepareLedger(sessionId, kind, page, summary) {
  const file = ledgerPath(sessionId, kind);
  const workflow = await readJson(runPath(sessionId));
  if (!workflow?.workPageName || !workflow?.workPage?.targetId) throw new Error("utm-business work page binding is missing before ledger creation");
  await assertBoundWorkPage(page, workflow.workPageName, workflow.workPage.targetId);
  const pageIdentity = {
    url: safeUrl(page.url()), titleHash: hash(await page.title()),
    workPageNameHash: hash(workflow.workPageName), targetIdHash: hash(workflow.workPage.targetId),
  };
  const inputHash = hash({ summary, pageIdentity });
  const existing = await readJson(file);
  if (existing) {
    if (existing.contextId !== sessionId || existing.kind !== kind) {
      throw new Error(`${kind} ledger belongs to another context or action`);
    }
    if (existing.state !== "planned") {
      return { file, ledger: existing };
    }
    if (existing.inputHash !== inputHash) {
      const replanned = {
        ...existing,
        inputHash,
        pageIdentity,
        replannedAt: new Date().toISOString(),
      };
      await writeSecureJson(file, replanned);
      return { file, ledger: replanned };
    }
    return { file, ledger: existing };
  }
  const ledger = {
    attemptId: `${kind}-${hash({ contextId: sessionId, inputHash }).slice(0, 24)}`,
    kind, contextId: sessionId, inputHash, pageIdentity, state: "planned", createdAt: new Date().toISOString(),
  };
  await writeSecureJson(file, ledger);
  return { file, ledger };
}

async function updateLedger(file, ledger, state) {
  const updated = { ...ledger, state, updatedAt: new Date().toISOString() };
  await writeSecureJson(file, updated);
  return updated;
}

async function clickOnce({ page, sessionId, kind, button, summary, success }) {
  const prepared = await prepareLedger(sessionId, kind, page, summary);
  let { file, ledger } = prepared;
  if (ledger.state !== "planned") {
    if (await success()) return await updateLedger(file, ledger, "submitted");
    throw new Error(`${kind} attempt is ${ledger.state}; refusing a second click`);
  }
  const workflow = await readJson(runPath(sessionId));
  if (liveSessionId(await browserVersion()) !== workflow?.browserSessionId) {
    throw new Error("browser session changed before irreversible action");
  }
  await assertBoundWorkPage(page, workflow.workPageName, workflow.workPage.targetId);
  assertApplePage(page);
  ledger = await updateLedger(file, ledger, "clicking");
  try { await button.click({ timeout: SHORT_TIMEOUT }); }
  catch (error) {
    await updateLedger(file, ledger, "unknown");
    throw new Error(`${kind} click result is unknown: ${error.message}`);
  }
  try { await waitUntil(page, success, `${kind} result`); }
  catch (error) { await updateLedger(file, ledger, "unknown"); throw error; }
  return await updateLedger(file, ledger, "submitted");
}

async function openBusiness(page) {
  await safePage(page);
  const url = new URL(page.url());
  if (url.hostname !== "appstoreconnect.apple.com" || !/business/i.test(url.pathname)) {
    const business = await roleAction(page, "Business").catch(() => null);
    if (business) await business.click({ timeout: SHORT_TIMEOUT });
    else await page.goto(BUSINESS_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  await waitUntil(page, async () => {
    await safePage(page);
    return new URL(page.url()).hostname === "appstoreconnect.apple.com" &&
      (/business/i.test(new URL(page.url()).pathname) || /Tax Forms|Agreements|Bank Accounts/i.test(await bodyText(page)));
  }, "App Store Connect Business page");
  await waitForStableBusinessPage(page);
}

async function completeDsa(page, sessionId, run) {
  const body = await bodyText(page);
  let dialogs = await visible(page.getByRole("dialog"));
  let dialog = null;
  for (const candidate of dialogs) {
    if (/Digital Services Act Compliance/i.test(await candidate.innerText())) {
      if (dialog) throw new Error("multiple DSA dialogs are visible");
      dialog = candidate;
    }
  }
  if (!dialog && !/Complete Compliance Requirements/i.test(body)) {
    await checkpoint(run, sessionId, "dsa", { status: "not-pending" });
    return "existing";
  }
  if (!dialog) {
    await (await roleAction(page, "Complete Compliance Requirements")).click();
    await waitUntil(page, async () => (await visible(page.getByRole("dialog"))).length === 1, "DSA dialog");
    dialogs = await visible(page.getByRole("dialog"));
    dialog = await unique(dialogs, "DSA dialog");
  }
  const answer = "I'm not a trader under the DSA or I don't plan to distribute in the EU";
  await waitUntil(page, async () => {
    try {
      await controlByText(page, answer, "radio");
      return true;
    } catch {
      return false;
    }
  }, "DSA non-trader radio");
  await setControl(page, answer, "radio");
  const nextButtons = await visible(dialog.getByRole("button", { name: "Next", exact: true }));
  if (nextButtons.length === 1) {
    await nextButtons[0].click();
    await waitUntil(page, async () => {
      const currentDialogs = await visible(page.getByRole("dialog"));
      return currentDialogs.length === 0 || (currentDialogs.length === 1 &&
        (await visible(currentDialogs[0].getByRole("button", { name: "Done", exact: true }))).length === 1);
    }, "DSA confirmation step");
  } else if (nextButtons.length > 1) throw new Error(`DSA Next must be unique; found ${nextButtons.length}`);
  const currentDialogs = await visible(page.getByRole("dialog"));
  if (currentDialogs.length === 1) {
    const done = await unique(await visible(currentDialogs[0].getByRole("button", { name: "Done", exact: true })), "DSA Done", { enabled: true });
    await done.click();
  } else if (currentDialogs.length > 1) throw new Error(`DSA confirmation dialog must be unique; found ${currentDialogs.length}`);
  await waitUntil(page, async () => !/Complete Compliance Requirements/i.test(await bodyText(page)) ||
    /\bCompleted\b|requirements have been completed/i.test(await bodyText(page)), "DSA completion");
  await checkpoint(run, sessionId, "dsa", { answer });
  return "completed";
}

async function confirmStandardizedLegalEntityAddress(page) {
  const matches = [];
  for (const candidate of await visible(page.getByRole("dialog"))) {
    if (/Review Legal Entity Address/i.test(await candidate.innerText())) matches.push(candidate);
  }
  if (!matches.length) return false;
  const dialog = await unique(matches, "Review Legal Entity Address dialog");
  const standardized = await controlByText(dialog, "Standardized Address", "radio");
  if (!await standardized.isChecked()) {
    throw new Error("Standardized Address is not selected by default");
  }
  const confirm = await unique(
    await visible(dialog.getByRole("button", { name: "Confirm", exact: true })),
    "Legal Entity address Confirm",
    { enabled: true },
  );
  await confirm.click({ timeout: SHORT_TIMEOUT });
  await waitUntil(page, async () => {
    for (const candidate of await visible(page.getByRole("dialog"))) {
      if (/Review Legal Entity Address/i.test(await candidate.innerText())) return false;
    }
    return true;
  }, "Legal Entity address confirmation");
  return true;
}

async function completeLegalEntity(page, sessionId, run) {
  if (await confirmStandardizedLegalEntityAddress(page)) {
    const file = ledgerPath(sessionId, "legal-entity-save");
    const ledger = await readJson(file);
    if (ledger) {
      const submitted = await updateLedger(file, ledger, "submitted");
      await checkpoint(run, sessionId, "legalEntity", { attemptId: submitted.attemptId, standardizedAddressConfirmed: true });
    }
    await waitForStableBusinessPage(page);
    return "completed";
  }
  if (await twoFactorPending(page)) {
    const file = ledgerPath(sessionId, "legal-entity-save");
    let ledger = await readJson(file);
    if (!ledger || !["clicking", "unknown"].includes(ledger.state)) {
      throw new Error("Legal Entity two-factor challenge has no recoverable save attempt");
    }
    await completeOtp(page);
    await waitUntil(page, async () => {
      const pending = await visible(page.getByRole("button", { name: "Edit Legal Entity", exact: true }));
      const dialogs = await visible(page.getByRole("dialog"));
      return pending.length === 0 && dialogs.length === 0;
    }, "Legal Entity saved state after two-factor authentication");
    ledger = await updateLedger(file, ledger, "submitted");
    await checkpoint(run, sessionId, "legalEntity", { attemptId: ledger.attemptId, recoveredAfterTwoFactor: true });
    await waitForStableBusinessPage(page);
    return "completed";
  }
  const staleFile = ledgerPath(sessionId, "legal-entity-save");
  const staleLedger = await readJson(staleFile);
  if (staleLedger && ["unknown", "failed"].includes(staleLedger.state)) {
    const pending = await visible(page.getByRole("button", { name: "Edit Legal Entity", exact: true }));
    const requirementStillVisible = /update your legal entity information prior to signing the Paid Apps Agreement/i.test(await bodyText(page));
    if (pending.length !== 1 || !requirementStillVisible || await twoFactorPending(page)) {
      throw new Error("the prior Legal Entity save result is not proven failed; refusing a retry");
    }
    if (!/^legal-entity-save-[a-f0-9]{24}$/i.test(staleLedger.attemptId || "")) {
      throw new Error("the prior Legal Entity attempt ID is invalid");
    }
    const archiveFile = path.join(sessionDir(sessionId), `legal-entity-save-attempt.${staleLedger.attemptId}.failed.json`);
    const archived = await readJson(archiveFile);
    if (archived && archived.attemptId !== staleLedger.attemptId) throw new Error("Legal Entity failed-attempt archive conflicts");
    const failed = {
      ...staleLedger, state: "failed", failedAt: new Date().toISOString(),
      failureEvidenceHash: hash({ requirementStillVisible, editActionCount: pending.length, twoFactor: false }),
    };
    await writeSecureJson(staleFile, failed);
    await rename(staleFile, archiveFile);
    run.recovery ||= {};
    run.recovery.legalEntityRetryOf = staleLedger.attemptId;
    await writeSecureJson(runPath(sessionId), run);
  }
  let dialogs = await visible(page.getByRole("dialog"));
  let dialog = null;
  for (const candidate of dialogs) {
    if (/Edit Legal Entity/i.test(await candidate.innerText())) {
      if (dialog) throw new Error("multiple Edit Legal Entity dialogs are visible");
      dialog = candidate;
    }
  }
  if (!dialog) {
    const actions = await visible(page.getByRole("button", { name: "Edit Legal Entity", exact: true }));
    if (!actions.length) {
      await checkpoint(run, sessionId, "legalEntity", { status: "not-pending" });
      return "existing";
    }
    const action = await unique(actions, "Edit Legal Entity", { enabled: true });
    await action.click({ timeout: SHORT_TIMEOUT });
    await waitUntil(page, async () => (await visible(page.getByRole("dialog"))).length === 1, "Edit Legal Entity dialog");
    dialog = await unique(await visible(page.getByRole("dialog")), "Edit Legal Entity dialog");
  }
  await waitUntil(page, async () =>
    (await visible(dialog.locator('[name="financeName"]'))).length === 1,
  "Legal Entity fields ready", SHORT_TIMEOUT);
  const requiredNames = [
    "financeName", "isOrganization", "financeAddress.addressLine1", "financeAddress.city",
    "financeAddress.postalCode", "financeAddress.country",
  ];
  const evidence = [];
  for (const name of requiredNames) {
    const control = await unique(await visible(dialog.locator(`[name="${name}"]`)), `Legal Entity ${name}`);
    const value = (await control.inputValue()).trim();
    if (!value || await control.getAttribute("aria-invalid") === "true") {
      throw new Error(`Legal Entity ${name} is empty or invalid`);
    }
    evidence.push({ name, valueHash: hash(value), disabled: await control.isDisabled() });
  }
  for (const optionalName of ["financeAddress.addressLine2", "financeAddress.regionText"]) {
    const control = await unique(await visible(dialog.locator(`[name="${optionalName}"]`)), `Legal Entity ${optionalName}`);
    if (await control.getAttribute("aria-invalid") === "true") throw new Error(`Legal Entity ${optionalName} is invalid`);
    const value = (await control.inputValue()).trim();
    evidence.push({ name: optionalName, valueHash: value ? hash(value) : null, optional: true });
  }
  const save = await unique(await visible(dialog.getByRole("button", { name: "Save", exact: true })), "Legal Entity Save", { enabled: true });
  const summary = {
    form: "legal-entity", fields: evidence, page: safeUrl(page.url()),
    retryOf: run.recovery?.legalEntityRetryOf || null,
  };
  const ledger = await clickOnce({
    page, sessionId, kind: "legal-entity-save", button: save, summary,
    success: async () => {
      if (await twoFactorPending(page)) await completeOtp(page);
      await confirmStandardizedLegalEntityAddress(page);
      const openDialogs = await visible(page.getByRole("dialog"));
      const pending = await visible(page.getByRole("button", { name: "Edit Legal Entity", exact: true }));
      return openDialogs.length === 0 && pending.length === 0;
    },
  });
  await checkpoint(run, sessionId, "legalEntity", { attemptId: ledger.attemptId, ledgerHash: hash(summary) });
  await waitForStableBusinessPage(page);
  return "completed";
}

async function completePaidAgreement(page, sessionId, run) {
  await openBusiness(page);
  const status = await itemRowStatus(page, "Paid Apps Agreement");
  if (status === "Active") {
    await checkpoint(run, sessionId, "paidAgreement", { status: "Active" });
    return "existing";
  }
  if (!/Sign the Paid Apps Agreement/i.test(await bodyText(page))) {
    await checkpoint(run, sessionId, "paidAgreement", { status: "not-pending" });
    return "existing";
  }
  await (await roleAction(page, "Sign the Paid Apps Agreement")).click();
  const boxes = await visible(page.locator("input[type='checkbox']"));
  const box = await unique(boxes, "Paid Apps agreement checkbox");
  if (!await box.isChecked()) await box.check();
  const agree = await roleAction(page, "Agree");
  const ledger = await clickOnce({
    page, sessionId, kind: "paid-apps-agreement", button: agree,
    summary: { checked: await box.isChecked(), page: safeUrl(page.url()) },
    success: async () => {
      if (/Two-Factor Authentication Required|verification code/i.test(await bodyText(page))) {
        await completeOtp(page);
      }
      return !/Sign the Paid Apps Agreement/i.test(await bodyText(page)) &&
        !/Two-Factor Authentication Required|verification code/i.test(await bodyText(page));
    },
  });
  await checkpoint(run, sessionId, "paidAgreement", { attemptId: ledger.attemptId });
  return "completed";
}

async function answerNoQuestion(page, scope, question, finalButton) {
  const marker = await unique(await visible(scope.getByText(question)), String(question));
  const container = marker.locator("xpath=ancestor::*[self::fieldset or @role='group' or self::section or self::div][1]");
  let noBoxes = await visible(container.getByLabel("No", { exact: true }));
  if (!noBoxes.length) noBoxes = await visible(container.locator("input[type='radio']").filter({ has: page.getByText("No", { exact: true }) }));
  const no = noBoxes.length === 1 ? noBoxes[0] : await controlByText(page, "No", "radio");
  if (!await no.isChecked()) await no.check();
  if (!await no.isChecked()) throw new Error(`${question} No answer did not persist`);
  const next = await unique(await visible(scope.getByRole("button", { name: finalButton, exact: true })), finalButton, { enabled: true });
  await next.click();
}

async function ensureTaxQuestionnaire(page, sessionId, run) {
  const residentQuestionText = /Are you considered a U\.S\. (?:tax )?resident\?/i;
  await openBusiness(page);
  await page.getByText("Tax Forms", { exact: true }).first().scrollIntoViewIfNeeded().catch(() => {});
  const text = await bodyText(page);
  if (/U\.S\. Certificate of Foreign Status of Beneficial Owner/i.test(text) && /U\.S\. Form W-8BEN/i.test(text)) {
    await checkpoint(run, sessionId, "taxQuestionnaire", { status: "forms-present" });
    return "existing";
  }
  const taxDialogLocator = page.getByRole("dialog").filter({ hasText: "U.S. Tax Questionnaire" });
  let taxDialogs = await visible(taxDialogLocator);
  if (!taxDialogs.length) await (await roleAction(page, "U.S. Tax Questionnaire")).click();
  await waitUntil(page, async () => {
    taxDialogs = await visible(taxDialogLocator);
    return taxDialogs.length === 1;
  }, "U.S. Tax Questionnaire dialog");
  const taxDialog = await unique(taxDialogs, "U.S. Tax Questionnaire dialog");
  await waitUntil(page, async () =>
    (await visible(taxDialog.getByText(residentQuestionText))).length === 1,
  "U.S. resident question");
  await answerNoQuestion(page, taxDialog, residentQuestionText, "Next");
  await waitUntil(page, async () =>
    (await visible(taxDialog.getByText("Do you have any U.S. Business Activities?", { exact: false }))).length === 1,
  "U.S. Business Activities question");
  await answerNoQuestion(page, taxDialog, "Do you have any U.S. Business Activities?", "Save");
  await waitUntil(page, async () => {
    const current = await bodyText(page);
    return /U\.S\. Certificate of Foreign Status of Beneficial Owner/i.test(current) && /U\.S\. Form W-8BEN/i.test(current);
  }, "two generated U.S. tax forms");
  await checkpoint(run, sessionId, "taxQuestionnaire", { resident: "No", activities: "No" });
  return "completed";
}

async function verifyRequiredFields(page) {
  const fields = await visible(page.locator("input[required], select[required], textarea[required], [aria-required='true']"));
  const evidence = [];
  for (const field of fields) {
    const value = "inputValue" in field ? await field.inputValue().catch(() => "") : "";
    const invalid = await field.getAttribute("aria-invalid");
    if (!String(value).trim() || invalid === "true") throw new Error("a required prefilled field is empty or invalid");
    evidence.push({ tag: await field.evaluate((node) => node.tagName), valueHash: hash(String(value)) });
  }
  return evidence;
}

async function completeForeignForm(page, sessionId, run) {
  await openBusiness(page);
  const rowStatus = await itemRowStatus(page, "U.S. Certificate of Foreign Status of Beneficial Owner");
  if (rowStatus === "Active") {
    await checkpoint(run, sessionId, "foreignForm", { status: "Active" });
    return { attemptId: "recovered-active", status: "existing" };
  }
  const businessText = await bodyText(page);
  if (/U\.S\. Certificate of Foreign Status of Beneficial Owner[^\n]*(Complete|Submitted)/i.test(businessText)) {
    await checkpoint(run, sessionId, "foreignForm", { status: "already-complete" });
    return { attemptId: "recovered-complete", status: "existing" };
  }
  const formHeading = page.getByRole("heading", { name: "U.S. Certificate of Foreign Status of Beneficial Owner", exact: true });
  if (!(await visible(formHeading)).length) await (await roleAction(page, "U.S. Certificate of Foreign Status of Beneficial Owner")).click();
  await waitUntil(page, async () => (await visible(formHeading)).length === 1, "Foreign Status form");
  await waitUntil(page, async () =>
    (await visible(page.getByLabel("Title", { exact: true }))).length === 1,
  "Title input", SHORT_TIMEOUT);
  const required = await verifyRequiredFields(page);
  await setControl(page, "I declare that the individual or organization", "checkbox");
  let titleItems = await visible(page.getByLabel("Title", { exact: true }));
  if (!titleItems.length) titleItems = await visible(page.locator("input[name*='title' i], input[id*='title' i]"));
  const title = await unique(titleItems, "Title input");
  await title.fill("CEO");
  if ((await title.inputValue()).trim() !== "CEO") throw new Error("Title did not persist as CEO");
  const submit = await roleAction(page, "Submit");
  const summary = { required, declaration: true, title: "CEO", form: "foreign-status", page: safeUrl(page.url()) };
  const ledger = await clickOnce({
    page, sessionId, kind: "foreign-form-submit", button: submit, summary,
    success: async () => /Business|Tax Forms/i.test(await bodyText(page)) && !/\bSubmit\b/.test(await bodyText(page)),
  });
  await openBusiness(page);
  await checkpoint(run, sessionId, "foreignForm", { attemptId: ledger.attemptId, ledgerHash: hash(summary) });
  return { attemptId: ledger.attemptId, status: "completed" };
}

function extractLatestSixDigitCode(text) {
  const normalized = String(text || "");
  const codes = Array.from(
    normalized.matchAll(/(?<!\d)(\d{6})(?!\d)/g),
    (match) => match[1],
  );
  return codes.at(-1) || null;
}

async function readMessages(smsPage) {
  const deadline = Date.now() + TIMEOUT;
  do {
    const text = await smsPage.locator("body").textContent({ timeout: SHORT_TIMEOUT }) || "";
    const code = extractLatestSixDigitCode(text);
    if (code) return [{ code }];
    if (Date.now() >= deadline) break;
    await smsPage.waitForTimeout(20);
  } while (Date.now() < deadline);
  return [];
}

async function openSmsInbox(page, smsUrl) {
  const accessCode = new URL(smsUrl).searchParams.get("code");
  const inputs = await visibleAcrossFrames(page, (frame) => frame.locator("input[type='text']"));
  if (inputs.length === 0 || !accessCode) return;
  if (inputs.length !== 1) throw new Error("SMS query input is not unique");
  await inputs[0].fill(accessCode);
  const submitters = inputs[0]
    .locator("xpath=ancestor::form[1]")
    .locator('button[type="submit"], input[type="submit"], button:not([type])');
  const enabledSubmitters = [];
  for (let index = 0; index < await submitters.count(); index += 1) {
    const submitter = submitters.nth(index);
    if (
      await submitter.isVisible().catch(() => false)
      && await submitter.isEnabled().catch(() => false)
    ) enabledSubmitters.push(submitter);
  }
  if (enabledSubmitters.length === 1) {
    await enabledSubmitters[0].click({ timeout: SHORT_TIMEOUT });
  } else {
    await inputs[0].press("Enter");
  }
  await inputs[0].waitFor({ state: "hidden", timeout: SHORT_TIMEOUT }).catch(() => {});
}

async function openSms(context, smsUrl) {
  const smsPage = await context.newPage();
  await smsPage.goto(smsUrl, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await openSmsInbox(smsPage, smsUrl);
  return smsPage;
}

async function waitNewestSms(smsPage, smsUrl) {
  let messages = await readMessages(smsPage);
  if (messages.length === 1) return messages[0];

  await smsPage.reload({ waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await openSmsInbox(smsPage, smsUrl);
  messages = await readMessages(smsPage);
  if (messages.length === 1) return messages[0];
  throw new Error("No six-digit verification code was readable after one SMS page refresh");
}

async function completeOtp(page) {
  if (!await twoFactorPending(page)) return;
  let phoneTail = String(otpInput?.phone || "").replace(/\D/g, "").slice(-2);
  if (phoneTail.length !== 2) throw new Error("Notion phone suffix is unavailable in guest memory");
  let smsUrl = String(otpInput?.smsUrl || "").trim();
  if (!smsUrl) throw new Error("Notion SMS URL is unavailable in guest memory");
  let smsPage = null;

  try {
    let inputs = await visibleAcrossFrames(page, (frame) =>
      frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
    if (!inputs.length) {
      const choices = await visibleAcrossFrames(page, (frame) => frame.getByRole("button"));
      const matches = [];
      for (const choice of choices) {
        const label = `${await choice.innerText()} ${await choice.getAttribute("aria-label") || ""}`;
        if (label.replace(/\D/g, "").endsWith(phoneTail)) matches.push(choice);
      }
      if (matches.length !== 1) throw new Error(`Apple phone-tail choice is not unique; found ${matches.length}`);
      await matches[0].click({ timeout: SHORT_TIMEOUT });
      await waitUntil(page, async () => {
        inputs = await visibleAcrossFrames(page, (frame) =>
          frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
        return inputs.length === 1 || inputs.length === 6;
      }, "six-digit verification input");
      await page.waitForTimeout(10_000);
    } else if (inputs.length !== 1 && inputs.length !== 6) {
      throw new Error(`verification code inputs must be one or six; found ${inputs.length}`);
    }

    smsPage = await openSms(page.context(), smsUrl);
    let latest = await waitNewestSms(smsPage, smsUrl);
    let code = latest.code;
    latest.code = "";

    await page.bringToFront();
    inputs = await visibleAcrossFrames(page, (frame) =>
      frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
    if (inputs.length === 1) await inputs[0].fill(code);
    else if (inputs.length === 6) {
      for (const input of inputs) await input.fill("");
      await inputs[0].focus();
      await inputs[0].pressSequentially(code);
    } else throw new Error(`verification code inputs must be one or six; found ${inputs.length}`);
    code = "";
    phoneTail = "";
    await waitUntil(page, async () => !await twoFactorPending(page), "Apple two-factor completion");
  } finally {
    if (smsPage && !smsPage.isClosed()) await smsPage.close().catch(() => {});
    smsUrl = "";
  }
}

function parseBirthday(value) {
  const match = value.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$|^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/);
  if (!match) throw new Error("birthday must be an unambiguous YYYY-MM-DD or MM-DD-YYYY value");
  const year = Number(match[1] || match[6]);
  const month = Number(match[2] || match[4]);
  const day = Number(match[3] || match[5]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month || date.getUTCDate() !== day) throw new Error("birthday is not a valid date");
  return { display: `${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}-${year}`, iso: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}` };
}

async function readStdinInput() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > 32_768) throw new Error("stdin input is too large");
    chunks.push(chunk);
  }
  let raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) throw new Error("utm-business stdin JSON is required");
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw new Error("stdin must be valid JSON"); }
  raw = "";
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("stdin must be a JSON object");
  const required = [
    "CONTEXT_ID", "VM_NAME", "APP_NAME", "BUNDLE_ID", "BIRTHDAY",
    "APPLE_ACCOUNT_PHONE", "APPLE_ACCOUNT_SMS_URL", "ABA_ROUTING_NUMBER",
    "ACCOUNT_NUMBER", "EXPECTED_EDGE_PID", "EXPECTED_EDGE_WEBSOCKET",
  ];
  const allowed = [...required, "STAGE"];
  const parsedKeys = Object.keys(parsed);
  if (required.some((key) => !parsedKeys.includes(key)) ||
      parsedKeys.some((key) => !allowed.includes(key))) {
    throw new Error("utm-business stdin JSON fields are invalid");
  }
  const contextId = String(parsed.CONTEXT_ID || "");
  const vmName = String(parsed.VM_NAME || "");
  const appName = String(parsed.APP_NAME || "").trim();
  const bundleId = String(parsed.BUNDLE_ID || "").trim();
  const birthday = parseBirthday(String(parsed.BIRTHDAY || "").trim());
  const phone = String(parsed.APPLE_ACCOUNT_PHONE || "").trim();
  const smsUrl = String(parsed.APPLE_ACCOUNT_SMS_URL || "").trim();
  const routingNumber = String(parsed.ABA_ROUTING_NUMBER || "").replace(/\s+/g, "");
  const accountNumber = String(parsed.ACCOUNT_NUMBER || "").replace(/\s+/g, "");
  const expectedEdgePid = parsed.EXPECTED_EDGE_PID;
  const expectedEdgeWebsocket = String(parsed.EXPECTED_EDGE_WEBSOCKET || "");
  const stage = String(parsed.STAGE || "all");
  if (!/^[A-Za-z0-9-]{8,80}$/.test(contextId) || !/^[a-z]{4}$/.test(vmName) ||
      !appName || !/^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$/.test(bundleId) ||
      phone.replace(/\D/g, "").length < 4 || !/^https?:\/\//.test(smsUrl) ||
      !/^\d{9}$/.test(routingNumber) || !/^\d{4,17}$/.test(accountNumber) ||
      !Number.isInteger(expectedEdgePid) || expectedEdgePid <= 0 ||
      !expectedEdgeWebsocket.startsWith("ws://127.0.0.1:9222/") || !["all", "bank"].includes(stage)) {
    throw new Error("utm-business stdin JSON values are invalid");
  }
  for (const key of allowed) parsed[key] = null;
  parsed = null;
  return {
    contextId, vmName, appName, bundleId, birthday, phone, smsUrl,
    routingNumber, accountNumber, expectedEdgePid, expectedEdgeWebsocket, stage,
  };
}

async function completeW8Ben(page, sessionId, run, birthday) {
  await openBusiness(page);
  const rowStatus = await itemRowStatus(page, "U.S. Form W-8BEN");
  if (rowStatus === "Active") {
    await checkpoint(run, sessionId, "w8ben", { status: "Active" });
    return { attemptId: "recovered-active", status: "existing" };
  }
  const formHeading = page.getByRole("heading", { name: "U.S. Form W-8BEN", exact: true });
  const formActive = (await visible(formHeading)).length === 1;
  if (!formActive && /U\.S\. Form W-8BEN[^\n]*(Complete|Submitted)/i.test(await bodyText(page))) {
    await checkpoint(run, sessionId, "w8ben", { status: "already-complete" });
    return { attemptId: "recovered-complete", status: "existing" };
  }
  if (!formActive) await (await roleAction(page, "U.S. Form W-8BEN")).click();
  await waitUntil(page, async () => (await visible(formHeading)).length === 1, "W-8BEN form");
  await waitUntil(page, async () => {
    const placeholder = await visible(page.getByPlaceholder("MM-DD-YYYY", { exact: true }));
    const named = await visible(page.locator("input[name*='birth' i], input[id*='birth' i]"));
    return placeholder.length === 1 || named.length === 1;
  }, "Date of Birth input", SHORT_TIMEOUT);
  let dateItems = await visible(page.getByPlaceholder("MM-DD-YYYY", { exact: true }));
  if (!dateItems.length) dateItems = await visible(page.locator("input[name*='birth' i], input[id*='birth' i]"));
  const dateInput = await unique(dateItems, "Date of Birth input");
  if (![birthday.iso, birthday.display].includes(await dateInput.inputValue())) {
    if (await dateInput.isDisabled()) {
      const open = await unique(await visible(page.getByLabel(/^(?:8\.\s*)?Date of Birth$/i)), "Date of Birth picker", { enabled: true });
      await open.click();
      const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
      const monthName = monthNames[Number(birthday.iso.slice(5, 7)) - 1];
      const year = birthday.iso.slice(0, 4);
      const selects = await visible(page.locator("select"));
      let monthSelect = null;
      let yearSelect = null;
      for (const select of selects) {
        const labels = await select.locator("option").allTextContents();
        if (labels.includes(monthName)) monthSelect = select;
        if (labels.includes(year)) yearSelect = select;
      }
      if (!monthSelect || !yearSelect) throw new Error("Date of Birth month/year controls are unavailable");
      await monthSelect.selectOption({ label: monthName });
      await yearSelect.selectOption({ label: year });
      const day = await unique(await visible(page.locator(`button[aria-label^="${birthday.iso}T"]`)), "Date of Birth day", { enabled: true });
      await day.click();
    } else {
      const type = await dateInput.getAttribute("type");
      await dateInput.fill(type === "date" ? birthday.iso : birthday.display);
    }
  }
  const readback = await dateInput.inputValue();
  if (![birthday.iso, birthday.display].includes(readback)) throw new Error("Date of Birth did not persist");
  await setControl(page, "I certify that the beneficial owner", "checkbox");
  await setControl(page, "Income from the sale of applications", "checkbox").catch(async () =>
    setControl(page, "Income from the sale of applications", "radio"));
  await setControl(page, "Under penalties of perjury", "checkbox");
  await setControl(page, "I certify that I have the capacity", "checkbox");
  const required = await verifyRequiredFields(page);
  const submit = await roleAction(page, "Submit");
  const summary = {
    form: "w8ben", birthdayHash: hash(birthday.iso), required,
    partII: true, applicationIncome: true, partIII: [true, true], page: safeUrl(page.url()),
  };
  const ledger = await clickOnce({
    page, sessionId, kind: "w8ben-submit", button: submit, summary,
    success: async () => /Business|Tax Forms/i.test(await bodyText(page)) && !/\bSubmit\b/.test(await bodyText(page)),
  });
  await openBusiness(page);
  await checkpoint(run, sessionId, "w8ben", { attemptId: ledger.attemptId, ledgerHash: hash(summary) });
  return { attemptId: ledger.attemptId, status: "completed" };
}

async function readAgreementEntity(page) {
  const headings = await visible(page.locator("h2"));
  const texts = [];
  for (const heading of headings) texts.push((await heading.innerText()).replace(/\s+/g, " ").trim());
  const agreementsIndex = texts.indexOf("Agreements");
  if (agreementsIndex < 1 || texts.indexOf("Agreements", agreementsIndex + 1) !== -1) {
    throw new Error("Agreements heading position is not unique");
  }
  const name = texts[agreementsIndex - 1];
  if (!name || name === "Business") throw new Error("agreement entity name is unavailable");
  const lines = await headings[agreementsIndex - 1].evaluate((start) => {
    const end = [...document.querySelectorAll("h2")].find((heading) =>
      heading.innerText.replace(/\s+/g, " ").trim() === "Agreements");
    if (!end) throw new Error("Agreements heading is unavailable");
    const values = [];
    for (const paragraph of document.querySelectorAll("p")) {
      const afterStart = Boolean(start.compareDocumentPosition(paragraph) & Node.DOCUMENT_POSITION_FOLLOWING);
      const beforeEnd = Boolean(paragraph.compareDocumentPosition(end) & Node.DOCUMENT_POSITION_FOLLOWING);
      const value = paragraph.innerText.replace(/\s+/g, " ").trim();
      if (afterStart && beforeEnd && value) values.push(value);
    }
    return [...new Set(values)];
  });
  if (lines.length < 4) throw new Error("agreement entity details are incomplete");
  return { name, lines };
}

async function processingMessageVisible(page) {
  return (await visible(page.getByText(BANK_PROCESSING_TEXT, { exact: true }))).length === 1;
}

async function completeBankAccount(page, sessionId, run, bankInput) {
  const ledgerFile = ledgerPath(sessionId, "bank-account-add");
  let existingLedger = await readJson(ledgerFile);
  if (existingLedger && existingLedger.contextId !== sessionId) throw new Error("bank account ledger belongs to another context");
  if (await twoFactorPending(page)) {
    if (!existingLedger || !["clicking", "unknown"].includes(existingLedger.state)) {
      throw new Error("bank account two-factor challenge has no recoverable Add attempt");
    }
    await completeOtp(page);
    await page.reload({ waitUntil: "domcontentloaded", timeout: TIMEOUT });
    await waitUntil(page, async () => await processingMessageVisible(page), "bank processing message after one refresh");
    existingLedger = await updateLedger(ledgerFile, existingLedger, "submitted");
    await checkpoint(run, sessionId, "bankAccount", { attemptId: existingLedger.attemptId, processing: true, refreshedOnce: true });
    return { attemptId: existingLedger.attemptId, status: "completed" };
  }

  await openBusiness(page);
  const addBankButtons = await visible(page.getByRole("button", { name: "Add Bank Account", exact: true }));
  if (!addBankButtons.length) {
    if (existingLedger && ["clicking", "unknown"].includes(existingLedger.state)) {
      existingLedger = await updateLedger(ledgerFile, existingLedger, "submitted");
    }
    await checkpoint(run, sessionId, "bankAccount", {
      attemptId: existingLedger?.attemptId || "recovered-no-add-action", addBankAccountVisible: false,
    });
    return {
      attemptId: existingLedger?.attemptId || "recovered-no-add-action",
      status: "existing",
    };
  }
  const bankHeading = await unique(await visible(page.getByRole("heading", { name: "Bank Accounts", exact: true })), "Bank Accounts heading");
  const bankSection = await unique(await visible(bankHeading.locator("xpath=ancestor::*[.//button[normalize-space()='Add Bank Account']][1]")), "Bank Accounts section");
  const addBank = await unique(await visible(bankSection.getByRole("button", { name: "Add Bank Account", exact: true })), "Bank Accounts Add Bank Account", { enabled: true });

  const entity = await readAgreementEntity(page);
  let holderDialogs = await visible(page.getByRole("dialog").filter({ hasText: "Account Holder Details" }));
  if (!holderDialogs.length) {
    await addBank.click();

    let countryDialogs = [];
    await waitUntil(page, async () => {
      countryDialogs = await visible(page.getByRole("dialog").filter({ hasText: "Add New Bank Account" }));
      return countryDialogs.length === 1;
    }, "Add New Bank Account country dialog");
    const countryDialog = await unique(countryDialogs, "Add New Bank Account country dialog");
    await waitUntil(page, async () =>
      (await visible(countryDialog.locator("select[name='selectedCountry']"))).length === 1,
    "Bank Country or Region select");
    const country = await fieldByLabel(countryDialog, "Bank Country or Region", "select[name='selectedCountry']");
    await country.selectOption({ label: "United States" });
    if ((await country.locator("option:checked").innerText()).trim() !== "United States") {
      throw new Error("Bank Country or Region did not persist as United States");
    }
    await (await enabledRoleAction(countryDialog, "Next", "bank country Next")).click();

    await waitUntil(page, async () => {
      holderDialogs = await visible(page.getByRole("dialog").filter({ hasText: "Account Holder Details" }));
      return holderDialogs.length === 1;
    }, "Account Holder Details dialog");
  }
  const holder = await unique(holderDialogs, "Account Holder Details dialog");
  await setControl(holder, "Same as Legal Entity", "checkbox");
  const holderName = await fieldByLabel(holder, "Account Holder Name", "input[name='accountHolderName']");
  await holderName.fill(entity.name);
  if ((await holderName.inputValue()).trim() !== entity.name) throw new Error("Account Holder Name does not match the current agreement entity");
  const holderType = await fieldByLabel(holder, "Account Holder Type", "select[name='accountHolderType']");
  await holderType.selectOption({ label: "Individual" });
  if ((await holderType.locator("option:checked").innerText()).trim() !== "Individual") {
    throw new Error("Account Holder Type did not persist as Individual");
  }
  const holderFieldNames = [
    "accountHolderName", "accountHolderType", "accountHolderAddress.addressLine1", "accountHolderAddress.addressLine2",
    "accountHolderAddress.city", "accountHolderAddress.regionSelect", "accountHolderAddress.postalCode", "accountHolderAddress.country",
  ];
  const holderValues = {};
  for (const name of holderFieldNames) {
    const field = holder.locator(`[name='${name}']`);
    if (await field.count() === 1) holderValues[name] = (await field.inputValue()).trim();
  }
  await writeSecureJson(bankEntityPath(sessionId), {
    source: "App Store Connect Business agreement entity", capturedAt: new Date().toISOString(),
    entity, accountHolder: holderValues,
  });
  await (await enabledRoleAction(holder, "Next", "Account Holder Details Next")).click();

  let detailsDialogs = [];
  await waitUntil(page, async () => {
    detailsDialogs = await visible(page.getByRole("dialog").filter({ hasText: "ABA Routing Number" }));
    return detailsDialogs.length === 1;
  }, "bank details dialog");
  const details = await unique(detailsDialogs, "bank details dialog");
  const selectedCountry = await fieldByLabel(details, "Country or Region", "select[name='selectedCountry']");
  if ((await selectedCountry.locator("option:checked").innerText()).trim() !== "United States") throw new Error("bank details country is not United States");
  const currency = await fieldByLabel(details, "Bank Account Currency", "select[name='selectedCurrency']");
  if (!/^USD\b/.test((await currency.locator("option:checked").innerText()).trim())) throw new Error("Bank Account Currency is not USD");
  const accountType = await fieldByLabel(details, "Account Type", "select[name='accountType']");
  await accountType.selectOption({ label: "Checking" });
  if ((await accountType.locator("option:checked").innerText()).trim() !== "Checking") throw new Error("Account Type did not persist as Checking");
  const nickname = await fieldByLabel(details, "Account Name", "input[name='nickName']");
  const routing = await fieldByLabel(details, "ABA Routing Number", "input[name='bankBranchIdentification1']");
  const account = await fieldByLabel(details, "Account Number", "input[name='accountNumber']");
  await nickname.fill(bankInput.appName);
  await routing.fill(bankInput.routingNumber);
  await account.fill(bankInput.accountNumber);
  const bankNumbersReadback = {
    routingNumber: String(await routing.inputValue()).replace(/\s+/g, ""),
    accountNumber: String(await account.inputValue()).replace(/\s+/g, ""),
  };
  if ((await nickname.inputValue()).trim() !== bankInput.appName ||
      bankNumbersReadback.routingNumber !== bankInput.routingNumber ||
      bankNumbersReadback.accountNumber !== bankInput.accountNumber) {
    throw new Error("bank page numbers do not exactly match current Notion values");
  }
  await (await enabledRoleAction(details, "Next", "bank details Next")).click();

  let certificationDialogs = [];
  await waitUntil(page, async () => {
    certificationDialogs = await visible(page.getByRole("dialog").filter({ hasText: "Certification" }));
    return certificationDialogs.length === 1;
  }, "bank Certification dialog");
  const certification = await unique(certificationDialogs, "bank Certification dialog");
  const statement = "I have read and agree to the terms and conditions above.";
  await setControl(certification, statement, "checkbox");
  const add = await enabledRoleAction(certification, "Add", "bank Certification Add");
  const summary = {
    entityHash: hash(entity), appNameHash: hash(bankInput.appName), routingHash: hash(bankInput.routingNumber),
    accountHash: hash(bankInput.accountNumber), bankCountry: "United States", currency: "USD",
    accountType: "Checking", sameAsLegalEntity: true, certification: true, page: safeUrl(page.url()),
  };
  let otpCompleted = false;
  let refreshedOnce = false;
  const ledger = await clickOnce({
    page, sessionId, kind: "bank-account-add", button: add, summary,
    success: async () => {
      if (await processingMessageVisible(page)) return true;
      if (await twoFactorPending(page)) {
        await completeOtp(page);
        otpCompleted = true;
      }
      if (otpCompleted && !refreshedOnce) {
        await page.reload({ waitUntil: "domcontentloaded", timeout: TIMEOUT });
        refreshedOnce = true;
      }
      return await processingMessageVisible(page);
    },
  });
  await checkpoint(run, sessionId, "bankAccount", {
    attemptId: ledger.attemptId, processing: true, refreshedOnce,
  });
  bankInput.routingNumber = "";
  bankInput.accountNumber = "";
  return { attemptId: ledger.attemptId, status: "completed" };
}

async function completeDac7(page, sessionId, run) {
  await openBusiness(page);
  const dac7Status = await itemRowStatus(page, "Directive on Administrative Cooperation - 7th Amendment");
  if (dac7Status === null) {
    await checkpoint(run, sessionId, "dac7", { status: "not-present", existing: true });
    return "existing";
  }
  const row = await textContainer(page, "Directive on Administrative Cooperation - 7th Amendment");
  const actions = [
    ...await visible(row.getByRole("button", { name: "Add Info", exact: true })),
    ...await visible(row.getByRole("link", { name: "Add Info", exact: true })),
  ];
  if (!actions.length) {
    const existing = [
      ...await visible(row.getByRole("button", { name: /Edit|View/i })),
      ...await visible(row.getByRole("link", { name: /Edit|View/i })),
    ];
    const action = await unique(existing, "DAC7 existing readback", { enabled: true });
    await action.click();
    const no = await controlByText(page, "No", "radio");
    if (!await no.isChecked()) throw new Error("DAC7 saved value is not No");
    const close = await roleAction(page, /Cancel|Close/, { exact: false }).catch(() => null);
    if (close) await close.click();
    await checkpoint(run, sessionId, "dac7", { value: "No", readback: true, existing: true });
    return "existing";
  }
  const addInfo = await unique(actions, "DAC7 Add Info", { enabled: true });
  await addInfo.click();
  await setControl(page, "No", "radio");
  await (await roleAction(page, "Done")).click();
  await waitUntil(page, async () => /Directive on Administrative Cooperation - 7th Amendment/i.test(await bodyText(page)), "DAC7 return");
  const reopened = await textContainer(page, "Directive on Administrative Cooperation - 7th Amendment");
  const action = await unique([
    ...await visible(reopened.getByRole("button", { name: /Add Info|Edit/i })),
    ...await visible(reopened.getByRole("link", { name: /Add Info|Edit/i })),
  ], "DAC7 readback action", { enabled: true });
  await action.click();
  const no = await controlByText(page, "No", "radio");
  if (!await no.isChecked()) throw new Error("DAC7 saved value is not No");
  const close = await roleAction(page, /Cancel|Close/, { exact: false }).catch(() => null);
  if (close) await close.click();
  await checkpoint(run, sessionId, "dac7", { value: "No", readback: true });
  return "completed";
}

async function selfTest() {
  const date = parseBirthday("2000-01-02");
  const testedBirthday = parseBirthday("1999/3/9");
  const name = workPageName("12345678-1234-1234-1234-123456789abc");
  const targetId = "abcdef1234567890";
  const fakePage = {
    isClosed: () => false,
    evaluate: async () => name,
    context: () => ({
      newCDPSession: async () => ({
        send: async () => ({ targetInfo: { targetId } }),
        detach: async () => {},
      }),
    }),
  };
  await assertBoundWorkPage(fakePage, name, targetId);
  let mismatchRejected = false;
  try { await assertBoundWorkPage(fakePage, name, "ffffffffffffffff"); } catch { mismatchRejected = true; }
  if (date.display !== "01-02-2000" || testedBirthday.display !== "03-09-1999" ||
      testedBirthday.iso !== "1999-03-09" || hash({ a: 1 }).length !== 64 ||
      !name.startsWith("codex-utm-business-") || !mismatchRejected) {
    throw new Error("self-test failed");
  }
  return { SELF_TEST: "verified" };
}

async function runUtmBusiness(input) {
  otpInput = { phone: input.phone, smsUrl: input.smsUrl };
  const bankInput = {
    appName: input.appName,
    routingNumber: input.routingNumber,
    accountNumber: input.accountNumber,
  };
  const birthday = input.birthday;
  const { page, contextId: sessionId, run, expectedName, expectedTargetId } = await connectSession(input);
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  await openBusiness(page);
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  const businessEntity = await readAgreementEntity(page);
  const businessText = [businessEntity.name, ...businessEntity.lines].join("\n");
  await checkpoint(run, sessionId, "business", { url: safeUrl(page.url()) });
  const foreignFormActive = (await visible(page.getByRole("heading", { name: "U.S. Certificate of Foreign Status of Beneficial Owner", exact: true }))).length === 1;
  const w8benActive = (await visible(page.getByRole("heading", { name: "U.S. Form W-8BEN", exact: true }))).length === 1;
  const bankFlowActive = (await visible(page.getByRole("dialog").filter({
    hasText: /Add New Bank Account|Account Holder Details|ABA Routing Number|Certification/,
  }))).length === 1;
  let dsaStatus = "existing";
  let legalEntityStatus = "existing";
  let paidAppsStatus = "existing";
  let taxQuestionnaireStatus = "existing";
  if (bankFlowActive) {
    reportExistingStep("UTM-Business DSA步骤");
    reportExistingStep("UTM-Business Legal Entity步骤");
    reportExistingStep("UTM-Business Paid Apps Agreement步骤");
    reportExistingStep("UTM-Business U.S. Tax Questionnaire步骤");
  } else if (!foreignFormActive && !w8benActive) {
    dsaStatus = await runReportedStep(
      "UTM-Business DSA步骤",
      async () => await completeDsa(page, sessionId, run),
    );
    await assertBoundWorkPage(page, expectedName, expectedTargetId);
    legalEntityStatus = await runReportedStep(
      "UTM-Business Legal Entity步骤",
      async () => await completeLegalEntity(page, sessionId, run),
    );
    await assertBoundWorkPage(page, expectedName, expectedTargetId);
    paidAppsStatus = await runReportedStep(
      "UTM-Business Paid Apps Agreement步骤",
      async () => await completePaidAgreement(page, sessionId, run),
    );
    await assertBoundWorkPage(page, expectedName, expectedTargetId);
    taxQuestionnaireStatus = await runReportedStep(
      "UTM-Business U.S. Tax Questionnaire步骤",
      async () => await ensureTaxQuestionnaire(page, sessionId, run),
    );
    await assertBoundWorkPage(page, expectedName, expectedTargetId);
  } else {
    reportExistingStep("UTM-Business DSA步骤");
    reportExistingStep("UTM-Business Legal Entity步骤");
    reportExistingStep("UTM-Business Paid Apps Agreement步骤");
    reportExistingStep("UTM-Business U.S. Tax Questionnaire步骤");
  }
  let foreignResult;
  if (bankFlowActive || w8benActive) {
    reportExistingStep("UTM-Business Foreign Status步骤");
    foreignResult = { attemptId: "recovered-complete", status: "existing" };
  } else {
    foreignResult = await runReportedStep(
      "UTM-Business Foreign Status步骤",
      async () => await completeForeignForm(page, sessionId, run),
    );
  }
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  let w8benResult;
  if (bankFlowActive) {
    reportExistingStep("UTM-Business W-8BEN步骤");
    w8benResult = { attemptId: "recovered-complete", status: "existing" };
  } else {
    w8benResult = await runReportedStep(
      "UTM-Business W-8BEN步骤",
      async () => await completeW8Ben(page, sessionId, run, birthday),
    );
  }
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  const bankResult = await runReportedStep(
    "UTM-Business 银行账户步骤",
    async () => await completeBankAccount(page, sessionId, run, bankInput),
  );
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  const dac7Status = await runReportedStep(
    "UTM-Business DAC7步骤",
    async () => await completeDac7(page, sessionId, run),
  );
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  run.workPage = {
    ...run.workPage, state: "bound", targetId: expectedTargetId, targetIdHash: hash(expectedTargetId),
    nameHash: hash(expectedName), lastUrl: safeUrl(page.url()),
    verifiedAt: new Date().toISOString(),
  };
  run.pages.work = {
    state: "bound", targetIdHash: hash(expectedTargetId), nameHash: hash(expectedName),
    lastUrl: safeUrl(page.url()), verifiedAt: new Date().toISOString(),
  };
  run.state = "verified";
  run.completedAt = new Date().toISOString();
  await writeSecureJson(runPath(sessionId), run);
  const identity = await (await loadSessionModule()).currentSessionIdentity();
  if (identity.edge_pid !== input.expectedEdgePid ||
      identity.edge_websocket !== input.expectedEdgeWebsocket) {
    throw new Error("the existing Edge session changed before completion");
  }
  return {
    LOCAL_BROWSER_SESSION: "verified", UTM_BUSINESS_WORK_PAGE: "bound",
    EDGE_EXISTING_PID: "verified", EDGE_PID: identity.edge_pid,
    EDGE_WEBSOCKET: identity.edge_websocket, BUSINESS_TEXT: businessText,
    BUSINESS_RESULT: "verified", DSA_COMPLIANCE: "verified",
    PAID_APPS_AGREEMENT: "accepted_or_existing", US_TAX_QUESTIONNAIRE: "No_No_saved",
    DSA_STATUS: dsaStatus, LEGAL_ENTITY_STATUS: legalEntityStatus,
    PAID_APPS_STATUS: paidAppsStatus, TAX_QUESTIONNAIRE_STATUS: taxQuestionnaireStatus,
    FOREIGN_FORM_SUBMIT_ATTEMPT_ID: foreignResult.attemptId,
    FOREIGN_STATUS_FORM_STATUS: foreignResult.status,
    W8BEN_SUBMIT_ATTEMPT_ID: w8benResult.attemptId, W8BEN_STATUS: w8benResult.status,
    FOREIGN_STATUS_FORM: "verified", W8BEN_SUBMIT: "verified",
    BANK_ACCOUNT_ADD_ATTEMPT_ID: bankResult.attemptId, BANK_ACCOUNT_STATUS: bankResult.status,
    BANK_ACCOUNT_PROCESSING: "verified", DAC7_READBACK: "No_saved",
    DAC7_STATUS: dac7Status,
    UTM_BUSINESS: "verified",
  };
}

async function runBankStage(input) {
  otpInput = { phone: input.phone, smsUrl: input.smsUrl };
  const bankInput = {
    appName: input.appName,
    routingNumber: input.routingNumber,
    accountNumber: input.accountNumber,
  };
  const { page, contextId: sessionId, run, expectedName, expectedTargetId } = await connectSession(input);
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  const bankResult = await completeBankAccount(page, sessionId, run, bankInput);
  await assertBoundWorkPage(page, expectedName, expectedTargetId);
  const identity = await (await loadSessionModule()).currentSessionIdentity();
  if (identity.edge_pid !== input.expectedEdgePid ||
      identity.edge_websocket !== input.expectedEdgeWebsocket) {
    throw new Error("the existing Edge session changed during bank stage");
  }
  return {
    EDGE_EXISTING_PID: "verified",
    EDGE_PID: identity.edge_pid,
    EDGE_WEBSOCKET: identity.edge_websocket,
    BANK_ACCOUNT_STATUS: bankResult.status,
    UTM_BUSINESS_BANK_STAGE: "verified",
  };
}

async function runWithStdin() {
  const input = await readStdinInput();
  try {
    if (input.stage === "bank") return await runBankStage(input);
    return await runUtmBusiness(input);
  }
  finally {
    input.birthday.display = ""; input.birthday.iso = "";
    input.phone = ""; input.smsUrl = ""; input.routingNumber = ""; input.accountNumber = "";
    otpInput = null;
  }
}

const selfTestOnly = process.argv.length === 3 && process.argv[2] === "--self-test";
if ((!selfTestOnly && process.argv.length !== 2) || (selfTestOnly && process.argv.length !== 3)) {
  process.stderr.write("UTM_BUSINESS_ERROR=Usage: node utm_business_one.mjs\n", () => process.exit(1));
} else {
  (selfTestOnly ? selfTest() : runWithStdin()).then(
    (result) => process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0)),
    (error) => process.stderr.write(`UTM_BUSINESS_ERROR=${String(error?.message || error).replace(/[\r\n]+/g, " ")}\n`, () => process.exit(1)),
  );
}
