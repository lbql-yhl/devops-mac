#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { chmod, mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";

async function loadSessionModule() {
  const candidates = [
    new URL("../../_shared/local-playwright/session.mjs", import.meta.url),
    new URL("./session.mjs", import.meta.url),
  ];
  let lastError = null;
  for (const candidate of candidates) {
    try {
      return await import(candidate.href);
    } catch (error) {
      if (error?.code !== "ERR_MODULE_NOT_FOUND") throw error;
      lastError = error;
    }
  }
  throw new Error(`local Playwright session module is unavailable: ${lastError?.message || "not found"}`);
}

const { connectLocalBrowser, secureScreenshot } = await loadSessionModule();

const TIMEOUT = 30_000;
const SHORT_TIMEOUT = 5_000;
const PROGRAM_URL = "https://developer.apple.com/app-store/small-business-program/";
const ENROLLMENT_PATH = "/app-store/small-business-program/enroll";
const PROJECT_ROOT = process.cwd();
const RUNTIME_ROOT = path.join(PROJECT_ROOT, "runtime", "utm-11");
const LEGACY_RUN_PATH = path.join(RUNTIME_ROOT, "run.json");
const LEGACY_LEDGER_PATH = path.join(RUNTIME_ROOT, "enrollment-attempt.json");
const SCREENSHOT_PATH = path.join(os.homedir(), "Downloads", "1.png");
const PROFILE_DIR = process.env.LOCAL_BROWSER_PROFILE_DIR ||
  path.join(os.homedir(), ".codex", "browser-profiles", "apple-developer");
const STATE_PATH = process.env.LOCAL_BROWSER_STATE_FILE || `${PROFILE_DIR}.session.json`;
const CDP_PORT = Number(process.env.LOCAL_BROWSER_CDP_PORT || "9222");

const ANSWERS = [
  { name: "paidAppsAgreement", answer: "Yes, I have accepted." },
  { name: "youMajorityPartnership", answer: "No" },
  { name: "anotherMajorityPartnership", answer: "No" },
  { name: "youUltimateDecisionMaking", answer: "No" },
  { name: "anotherUltimateDecisionMaking", answer: "No" },
];

function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

function assertDeveloperUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== "developer.apple.com") {
    throw new Error(`Unexpected page: ${safeUrl(value)}`);
  }
  return url;
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

function ledgerPath(sessionOrRun) {
  return path.join(sessionRuntimeDir(sessionOrRun), "enrollment-attempt.json");
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

async function connectExistingUtm10Session() {
  const [info, state] = await Promise.all([browserVersion(), readJson(STATE_PATH)]);
  const liveSessionId = sessionIdFromVersion(info);
  if (!info || !state || !liveSessionId) {
    throw new Error("utm-10 browser is not running; refusing to start another browser");
  }
  if (state.sessionId !== liveSessionId) throw new Error("utm-10 browser session changed");

  const connected = await connectLocalBrowser();
  if (connected.sessionId !== liveSessionId) throw new Error("connected browser session does not match utm-10");

  let existingRun = await readJson(runPath(liveSessionId));
  if (!existingRun) {
    const legacyRun = await readJson(LEGACY_RUN_PATH);
    if (legacyRun?.sessionId === liveSessionId) existingRun = legacyRun;
  }
  const run = existingRun || {
    runId: randomUUID(),
    sessionId: liveSessionId,
    createdAt: new Date().toISOString(),
    state: "started",
  };
  if (!await readJson(runPath(run))) await writeSecureJson(runPath(run), run);
  return { context: connected.context, run };
}

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

async function waitForSafePage(page) {
  await page.locator("body").waitFor({ state: "visible", timeout: TIMEOUT });
  if ((await visibleInFrames(page, (frame) => frame.getByText(/captcha|verify you are human|unusual activity/i))).length) {
    throw new Error("Apple security challenge detected");
  }
  if ((await visibleInFrames(page, (frame) => frame.locator(
    "input#account_name_text_field, input[autocomplete~='username'], input[type='password']",
  ))).length) {
    throw new Error("Apple login is required; rerun utm-10 first");
  }
}

function findEnrollmentPage(context) {
  return context.pages().findLast((page) => {
    try {
      const url = new URL(page.url());
      return url.hostname === "developer.apple.com" && url.pathname.startsWith(ENROLLMENT_PATH);
    } catch {
      return false;
    }
  }) || null;
}

async function openProgramPage(context, run) {
  let page = context.pages().find((candidate) => safeUrl(candidate.url()) === safeUrl(PROGRAM_URL));
  if (!page) {
    page = await context.newPage();
    await page.goto(PROGRAM_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  }
  assertDeveloperUrl(page.url());
  await page.bringToFront();
  await waitForSafePage(page);
  run.pages = { ...(run.pages || {}), program: safeUrl(page.url()) };
  await writeSecureJson(runPath(run), run);
  return page;
}

async function openEnrollment(context, run) {
  const existing = findEnrollmentPage(context);
  if (existing) {
    await existing.bringToFront();
    return existing;
  }

  const page = await openProgramPage(context, run);
  const marker = await exactlyOne(
    page,
    (frame) => frame.getByText("Get started today.", { exact: true }),
    "Get started today section",
  );
  const section = marker.locator("xpath=ancestor::*[self::section or self::div][1]");
  let enrollTargets = [
    ...await visible(section.getByRole("link", { name: "Enroll now", exact: true })),
    ...await visible(section.getByRole("button", { name: "Enroll now", exact: true })),
  ];
  if (!enrollTargets.length) {
    enrollTargets = [
      ...await visibleInFrames(page, (frame) => frame.getByRole("link", { name: "Enroll now", exact: true })),
      ...await visibleInFrames(page, (frame) => frame.getByRole("button", { name: "Enroll now", exact: true })),
    ];
  }
  if (enrollTargets.length !== 1) throw new Error(`Enroll now must be unique; found ${enrollTargets.length}`);

  const popupPromise = context.waitForEvent("page", { timeout: TIMEOUT }).catch(() => null);
  await enrollTargets[0].click({ timeout: SHORT_TIMEOUT });
  const popup = await popupPromise;
  if (popup) await popup.waitForLoadState("domcontentloaded", { timeout: TIMEOUT }).catch(() => {});

  const deadline = Date.now() + TIMEOUT;
  let enrollment = popup;
  while (Date.now() < deadline) {
    enrollment = findEnrollmentPage(context) || enrollment;
    if (enrollment && new URL(enrollment.url()).pathname.startsWith(ENROLLMENT_PATH)) break;
    await page.waitForTimeout(20);
  }
  if (!enrollment) throw new Error("Enroll now did not open the enrollment page");
  assertDeveloperUrl(enrollment.url());
  await enrollment.bringToFront();
  await waitForSafePage(enrollment);
  run.pages = { ...(run.pages || {}), enrollment: safeUrl(enrollment.url()) };
  await writeSecureJson(runPath(run), run);
  return enrollment;
}

async function successIsVisible(page) {
  const thankYou = await visibleInFrames(
    page,
    (frame) => frame.getByText("Thank you for your submission.", { exact: true }),
  );
  const received = await visibleInFrames(
    page,
    (frame) => frame.getByText(
      /^We['’]ve received your App Store Small Business Program enrollment and will email you about your status soon\.$/,
    ),
  );
  return thankYou.length === 1 && received.length === 1;
}

async function radioAnswer(page, name, answer) {
  const inputs = await visibleInFrames(
    page,
    (frame) => frame.locator(`input[type="radio"][name="${name}"]`),
  );
  if (inputs.length !== 2) throw new Error(`${name} must contain exactly two visible answers`);

  const matches = [];
  for (const input of inputs) {
    const label = await input.evaluate((element) => {
      const explicit = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
      return (explicit?.innerText || element.closest("label")?.innerText || "").replace(/\s+/g, " ").trim();
    });
    if (label === answer) matches.push(input);
  }
  if (matches.length !== 1) throw new Error(`${name} answer ${answer} is not unique`);
  return { name, answer, target: matches[0], inputs };
}

async function chooseAnswers(page) {
  const selected = [];
  const first = await radioAnswer(page, ANSWERS[0].name, ANSWERS[0].answer);
  if (!await first.target.isChecked()) await first.target.check({ timeout: SHORT_TIMEOUT });
  selected.push(first);

  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    const counts = await Promise.all(ANSWERS.slice(1).map(async ({ name }) =>
      (await visibleInFrames(page, (frame) => frame.locator(`input[type="radio"][name="${name}"]`))).length,
    ));
    if (counts.every((count) => count === 2)) break;
    await page.waitForTimeout(20);
  }

  for (const item of ANSWERS.slice(1)) {
    const answer = await radioAnswer(page, item.name, item.answer);
    if (!await answer.target.isChecked()) await answer.target.check({ timeout: SHORT_TIMEOUT });
    selected.push(answer);
  }

  for (const item of selected) {
    const checked = [];
    for (const input of item.inputs) if (await input.isChecked()) checked.push(input);
    if (checked.length !== 1 || !await item.target.isChecked()) {
      throw new Error(`${item.name} answer did not persist`);
    }
  }
  return selected.map(({ name, answer }) => ({ name, answer }));
}

async function checkAttestation(page) {
  const checkbox = await exactlyOne(
    page,
    (frame) => frame.locator('input[type="checkbox"][name="chkPolicyAgree"]'),
    "truthfulness attestation",
  );
  if (!await checkbox.isChecked()) await checkbox.check({ timeout: SHORT_TIMEOUT });
  if (!await checkbox.isChecked()) throw new Error("truthfulness attestation did not persist");
}

async function accountHash(page) {
  const fields = {};
  for (const id of ["contact_name", "contact_email", "team_id"]) {
    const field = await exactlyOne(page, (frame) => frame.locator(`#${id}`), id);
    const value = (await field.inputValue()).trim();
    if (!value) throw new Error(`${id} is empty`);
    fields[id] = value;
  }
  return sha256(fields);
}

async function prepareLedger(run, page, answers, identityHash) {
  const inputSummaryHash = sha256({ answers, attestation: true, accountHash: identityHash });
  const existing = await readJson(ledgerPath(run));
  if (existing) {
    if (existing.sessionId !== run.sessionId || existing.inputSummaryHash !== inputSummaryHash) {
      throw new Error("existing enrollment ledger does not match this session or answers");
    }
    return existing;
  }

  const pageIdentity = {
    url: safeUrl(page.url()),
    title: await page.title(),
    accountHash: identityHash,
  };
  const details = {
    kind: "small-business-enrollment",
    sessionId: run.sessionId,
    pageIdentity,
    inputSummaryHash,
  };
  const ledger = {
    attemptId: `small-business-enrollment-${sha256(details).slice(0, 24)}`,
    ...details,
    state: "planned",
    createdAt: new Date().toISOString(),
  };
  await writeSecureJson(ledgerPath(run), ledger);
  return ledger;
}

async function updateLedger(ledger, state) {
  const updated = { ...ledger, state, updatedAt: new Date().toISOString() };
  await writeSecureJson(ledgerPath(ledger), updated);
  return updated;
}

async function waitForSuccess(page) {
  const deadline = Date.now() + TIMEOUT;
  while (Date.now() < deadline) {
    if (await successIsVisible(page)) return true;
    await page.waitForTimeout(50);
  }
  return await successIsVisible(page);
}

async function submitOnce(page, run) {
  const answers = await chooseAnswers(page);
  await checkAttestation(page);
  const identityHash = await accountHash(page);
  const submit = await exactlyOne(
    page,
    (frame) => frame.locator('input[type="submit"]#submit'),
    "Small Business Submit",
    { enabled: true },
  );
  let ledger = await prepareLedger(run, page, answers, identityHash);

  if (ledger.state !== "planned") {
    if (!await successIsVisible(page)) {
      throw new Error(`enrollment attempt is ${ledger.state}; refusing to submit again`);
    }
    return ledger;
  }

  const liveSessionId = sessionIdFromVersion(await browserVersion());
  if (liveSessionId !== run.sessionId) throw new Error("browser session changed before Submit");
  assertDeveloperUrl(page.url());
  ledger = await updateLedger(ledger, "clicking");
  try {
    await submit.click({ timeout: SHORT_TIMEOUT });
  } catch (error) {
    await updateLedger(ledger, "unknown");
    throw new Error(`Submit click result is unknown: ${error.message}`);
  }

  if (!await waitForSuccess(page)) {
    await updateLedger(ledger, "unknown");
    throw new Error("Submit was clicked once, but both success messages did not appear");
  }
  return await updateLedger(ledger, "submitted");
}

async function saveSuccessScreenshot(page) {
  await secureScreenshot(page, SCREENSHOT_PATH);
  const [metadata, contents] = await Promise.all([stat(SCREENSHOT_PATH), readFile(SCREENSHOT_PATH)]);
  const pngHeader = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (metadata.size <= 8 || !contents.subarray(0, 8).equals(pngHeader)) {
    throw new Error("success screenshot is not a valid non-empty PNG");
  }
  if ((metadata.mode & 0o777) !== 0o600) throw new Error("success screenshot mode is not 600");
}

async function runUtm11() {
  const { context, run } = await connectExistingUtm10Session();
  const page = await openEnrollment(context, run);
  await waitForSafePage(page);

  let ledger = await readJson(ledgerPath(run));
  if (!ledger) {
    const legacyLedger = await readJson(LEGACY_LEDGER_PATH);
    if (legacyLedger?.sessionId === run.sessionId) {
      ledger = legacyLedger;
      await writeSecureJson(ledgerPath(run), ledger);
    }
  }
  const existingBusiness = await successIsVisible(page);
  if (existingBusiness) {
    if (ledger && ledger.state !== "submitted") ledger = await updateLedger(ledger, "submitted");
  } else {
    ledger = await submitOnce(page, run);
  }

  if (!await successIsVisible(page)) throw new Error("Small Business success page is not visible");
  await saveSuccessScreenshot(page);
  run.state = "verified";
  run.completedAt = new Date().toISOString();
  run.pages = { ...(run.pages || {}), enrollment: safeUrl(page.url()) };
  await writeSecureJson(runPath(run), run);

  return {
    LOCAL_BROWSER_SESSION: "verified",
    LOCAL_BROWSER_SESSION_ID: run.sessionId,
    PAID_APPS_AGREEMENT: "verified_by_enrollment_success",
    QUESTIONNAIRE: "verified",
    SMALL_BUSINESS_SUCCESS_MESSAGES: "verified",
    ENROLLMENT_SUBMIT_ATTEMPT_ID: ledger?.attemptId || "recovered-success",
    REVIEW_SCREENSHOT_05: "verified",
    EXISTING_BUSINESS: existingBusiness,
    UTM_11: "verified",
  };
}

if (process.argv.length !== 2) {
  process.stderr.write("UTM_11_ERROR=Usage: node scripts/utm_11_one.mjs\n", () => process.exit(1));
} else {
  runUtm11().then(
    (result) => process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0)),
    (error) => process.stderr.write(`UTM_11_ERROR=${error.message}\n`, () => process.exit(1)),
  );
}
