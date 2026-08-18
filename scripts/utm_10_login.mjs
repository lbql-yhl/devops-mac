#!/usr/bin/env node

import process from "node:process";

const TIMEOUT = 30_000;
const STATE_TIMEOUT = 5_000;
const ACCOUNT_URL = "https://developer.apple.com/account/";

async function readCredentials() {
  let line = "";
  if (process.stdin.isTTY && typeof process.stdin.setRawMode === "function") {
    process.stderr.write("Paste credential JSON, then press Enter (input is hidden): ");
    process.stdin.setEncoding("utf8");
    process.stdin.setRawMode(true);
    process.stdin.resume();
    try {
      line = await new Promise((resolve, reject) => {
        const onData = (chunk) => {
          for (const character of chunk) {
            if (character === "\u0003") {
              process.stdin.off("data", onData);
              reject(new Error("Credential input cancelled"));
              return;
            }
            if (character === "\r" || character === "\n") {
              process.stdin.off("data", onData);
              resolve(line);
              return;
            }
            if (character === "\u007f") line = line.slice(0, -1);
            else line += character;
          }
        };
        process.stdin.on("data", onData);
      });
    } finally {
      process.stdin.setRawMode(false);
      process.stdin.pause();
      process.stderr.write("\n");
    }
  } else {
    process.stdin.setEncoding("utf8");
    for await (const chunk of process.stdin) line += chunk;
    line = line.split(/\r?\n/, 1)[0];
  }
  let value;
  try { value = JSON.parse(line); } catch { throw new Error("Credential input is not valid JSON"); }
  const fields = ["email", "password", "phone", "smsUrl", "userName"];
  if (!fields.every((field) => typeof value[field] === "string" && value[field].length > 0)) {
    throw new Error("Credential fields are incomplete");
  }
  return value;
}

function safeUrl(value) {
  const url = new URL(value);
  return `${url.protocol}//${url.host}${url.pathname}`;
}

async function visible(page, factory) {
  if (page.isClosed()) return [];
  const found = [];
  for (const frame of page.frames()) {
    let locator;
    let count;
    try {
      locator = factory(frame);
      count = await locator.count();
    } catch { continue; }
    for (let index = 0; index < count; index += 1) {
      const item = locator.nth(index);
      if (await item.isVisible().catch(() => false)) found.push(item);
    }
  }
  return found;
}

async function activeControls(page, factory) {
  const found = await visible(page, factory);
  const active = [];
  for (const item of found) {
    const usable = await item.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      if (!rect.width || !rect.height || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) < 0.01 || node.getAttribute("aria-hidden") === "true") return false;
      }
      const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return Boolean(top && (top === element || element.contains(top)));
    }).catch(() => false);
    if (usable) active.push(item);
  }
  return active;
}

async function controlLabel(control) {
  try {
    const [text, ariaLabel] = await Promise.all([
      control.innerText({ timeout: STATE_TIMEOUT }),
      control.getAttribute("aria-label", { timeout: STATE_TIMEOUT }),
    ]);
    return `${text} ${ariaLabel || ""}`;
  } catch {
    return null;
  }
}

async function phoneChoiceControls(page, phone = "") {
  const tail = String(phone || "").replace(/\D/g, "").slice(-2);
  const choices = await visible(page, (frame) => frame.getByRole("button"));
  const tailMatches = [];
  const structuredMatches = [];
  for (const choice of choices) {
    const label = await controlLabel(choice);
    if (label === null) continue;
    const digits = label.replace(/\D/g, "");
    if (tail && digits.endsWith(tail)) tailMatches.push(choice);
    if (digits.length >= 2 && /[•●·*Xx…]/.test(label)) structuredMatches.push(choice);
  }
  return tailMatches.length ? tailMatches : structuredMatches;
}

async function loginAction(page, label) {
  const deadline = Date.now() + TIMEOUT;
  do {
    const continues = await activeControls(
      page,
      (frame) => frame.getByRole("button", { name: /^Continue$/i }),
    );
    if (continues.length > 1) throw new Error(`${label} Continue control is not unique`);
    if (continues.length === 1 && await continues[0].isEnabled().catch(() => false)) return continues[0];

    const signIns = await activeControls(
      page,
      (frame) => frame.getByRole("button", { name: /^Sign In$/i }),
    );
    if (signIns.length > 1) throw new Error(`${label} Sign In control is not unique`);
    if (signIns.length === 1 && await signIns[0].isEnabled().catch(() => false)) return signIns[0];
    await page.waitForTimeout(20);
  } while (Date.now() < deadline);
  throw new Error(`${label} Continue or Sign In control is not uniquely ready`);
}

async function pageState(page) {
  if (page.isClosed()) return "closed";
  let currentUrl = null;
  try { currentUrl = new URL(page.url()); } catch {}
  for (const frame of page.frames()) {
    try {
      if (await frame.getByText("Membership details", { exact: true }).count()) return "account";
      if (await frame.getByText(/captcha|verify you are human|unusual activity/i).count()) return "captcha";
      if (await frame.getByText(/account.*locked|locked.*account|cannot be used|not active/i).count()) return "locked";
      if (await frame.getByText(/verification code digit|too many verification codes|enter the last code/i).count()) return "otp";
    } catch { continue; }
  }
  if (currentUrl?.hostname === "developer.apple.com" && currentUrl.pathname.startsWith("/account")) {
    const accountHeadings = await visible(page, (frame) => frame.getByRole("heading", { name: "Account", exact: true }));
    if (accountHeadings.length > 0) return "account";
  }
  if ((await activeControls(page, (frame) => frame.getByRole("button", { name: /^Trust$/i }))).length === 1) return "trust";
  const otpInputs = await visible(page, (frame) =>
    frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
  if (otpInputs.length === 1 || otpInputs.length === 6) return "otp";
  if ((await phoneChoiceControls(page)).length > 0) return "phone";
  const usernames = await activeControls(page, (frame) => frame.locator("input#account_name_text_field, input[autocomplete~='username']"));
  const passwords = await activeControls(page, (frame) => frame.locator("input[type='password']"));
  if (usernames.length === 1 || passwords.length === 1) return "login";
  if (currentUrl?.hostname === "developer.apple.com") return "account_loading";
  return "unknown";
}

async function waitState(page, accepted, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  do {
    const current = await pageState(page);
    if (accepted.has(current)) return current;
  } while (Date.now() < deadline);
  return await pageState(page);
}

async function accountEmailMatches(page, expectedEmail) {
  const expected = String(expectedEmail || "").trim().toLocaleLowerCase("en-US");
  if (!expected) return false;
  for (const frame of page.frames()) {
    const body = String(await frame.locator("body").innerText().catch(() => ""))
      .toLocaleLowerCase("en-US");
    if (body.includes(expected)) return true;
  }
  return false;
}

async function accountUserNameMatches(page, expectedUserName) {
  const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim()
    .toLocaleLowerCase("en-US");
  const expected = normalize(expectedUserName);
  if (!expected) return false;
  for (const frame of page.frames()) {
    const body = String(await frame.locator("body").innerText().catch(() => ""));
    const lines = body.split(/\r?\n/).map(normalize).filter(Boolean);
    if (lines.includes(expected)) return true;
  }
  return false;
}

async function accountIdentityMatches(page, credentials) {
  return await accountEmailMatches(page, credentials.email)
    || await accountUserNameMatches(page, credentials.userName);
}

async function waitForAccountIdentity(page, credentials) {
  const deadline = Date.now() + TIMEOUT;
  do {
    if (await accountIdentityMatches(page, credentials)) return true;
    await page.waitForTimeout(20);
  } while (Date.now() < deadline);
  return await accountIdentityMatches(page, credentials);
}

function emitVerifiedLogin(page, sessionId, existingAccount = false) {
  process.stdout.write(`${JSON.stringify({
    LOCAL_BROWSER_SESSION: "verified",
    LOCAL_BROWSER_SESSION_ID: sessionId,
    APPLE_DEVELOPER_ACCOUNT_PAGE: "verified",
    APPLE_ACCOUNT: "verified",
    APPLE_ACCOUNT_EMAIL: "matched",
    EXISTING_ACCOUNT: existingAccount,
    UTM_10: "verified",
    url: safeUrl(page.url()),
  })}\n`, () => process.exit(0));
}

async function currentApplePage() {
  const connected = await connectLocalBrowser();
  const pages = connected.context.pages().filter((page) => {
    try { return new URL(page.url()).hostname.endsWith("apple.com"); } catch { return false; }
  });
  if (!pages.length) {
    const opened = await approvedPage(ACCOUNT_URL);
    return { ...opened, sessionId: connected.sessionId };
  }
  const states = await Promise.all(pages.map(pageState));
  const rank = { account: 0, trust: 1, otp: 2, phone: 3, account_loading: 4, login: 5, unknown: 6 };
  const chosen = states.map((value, index) => ({ value, index })).sort((a, b) => (rank[a.value] ?? 9) - (rank[b.value] ?? 9))[0].index;
  for (let index = 0; index < pages.length; index += 1) {
    if (index !== chosen && states[index] === "login" && states[chosen] !== "login") await pages[index].close();
  }
  await pages[chosen].bringToFront();
  return { context: connected.context, page: pages[chosen], sessionId: connected.sessionId };
}

function extractLatestSixDigitCode(text) {
  const normalized = String(text || "");
  if (/^\s*no\|/i.test(normalized)) return null;
  const codes = Array.from(
    normalized.matchAll(/(?<!\d)(\d{6})(?!\d)/g),
    (match) => match[1],
  );
  return codes.at(-1) || null;
}

async function readMessages(smsPage) {
  const deadline = Date.now() + STATE_TIMEOUT;
  do {
    const text = await smsPage.locator("body").textContent({ timeout: STATE_TIMEOUT }) || "";
    const code = extractLatestSixDigitCode(text);
    if (code) return [{ code }];
    if (Date.now() >= deadline) break;
    await smsPage.waitForTimeout(20);
  } while (Date.now() < deadline);
  return [];
}

async function openSmsInbox(page, smsUrl) {
  const accessCode = new URL(smsUrl).searchParams.get("code");
  const inputs = await activeControls(page, (frame) => frame.locator("input[type='text']"));
  if (inputs.length === 0) return;
  if (!accessCode) return;
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
    await enabledSubmitters[0].click({ timeout: STATE_TIMEOUT });
  } else {
    await inputs[0].press("Enter");
  }
  await inputs[0].waitFor({ state: "hidden", timeout: STATE_TIMEOUT }).catch(() => {});
}

async function openSms(context, smsUrl) {
  const page = await context.newPage();
  await page.goto(smsUrl, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await openSmsInbox(page, smsUrl);
  return page;
}

async function waitNewestSms(smsPage, smsUrl) {
  let messages = await readMessages(smsPage);
  if (messages.length === 1) return messages[0];

  await smsPage.reload({ waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await openSmsInbox(smsPage, smsUrl);
  messages = await readMessages(smsPage);
  if (messages.length === 1) return messages[0];
  throw new Error("No Apple Account Code was readable after one SMS page refresh");
}

if (process.argv.includes("--help")) {
  process.stdout.write("Usage: node scripts/utm_10_one.mjs\nThen paste one hidden JSON line with: email, password, phone, smsUrl\n");
  process.exit(0);
}
if (process.argv.includes("--self-test")) {
  const plainTextFixture = "验证码：045474。";
  if (extractLatestSixDigitCode(plainTextFixture) !== "045474") {
    throw new Error("plain text SMS parser self-test failed");
  }
  process.stdout.write("UTM_10_ONE_SCRIPT=self-test-passed\n");
  process.exit(0);
}

const { approvedPage, connectLocalBrowser, restartLocalBrowser } = await import(new URL("./session.mjs", import.meta.url));

async function completeLogin(page, credentials) {
  let usernames = await activeControls(page, (frame) => frame.locator("input#account_name_text_field, input[autocomplete~='username']"));
  let passwords = await activeControls(page, (frame) => frame.locator("input[type='password']"));

  if (usernames.length === 1 && passwords.length === 0) {
    await usernames[0].fill(credentials.email);
    if (await usernames[0].inputValue() !== credentials.email) throw new Error("Email did not fill correctly");

    const continueButton = await loginAction(page, "Email");
    await continueButton.click({ timeout: STATE_TIMEOUT });

    const passwordDeadline = Date.now() + TIMEOUT;
    do {
      passwords = await activeControls(page, (frame) => frame.locator("input[type='password']"));
      if (passwords.length === 1) break;
    } while (Date.now() < passwordDeadline);
  }

  if (passwords.length !== 1) throw new Error("Password field did not appear within 30 seconds");
  await passwords[0].fill(credentials.password);
  if (await passwords[0].inputValue() !== credentials.password) throw new Error("Password did not fill correctly");

  const checks = await activeControls(page, (frame) => frame.locator("input#remember-me[type='checkbox'], input#rememberMe[type='checkbox']"));
  if (checks.length === 1 && !await checks[0].isChecked()) await checks[0].check({ force: true, timeout: STATE_TIMEOUT });

  const signInButton = await loginAction(page, "Password");
  await signInButton.click({ timeout: STATE_TIMEOUT });

  return await waitState(
    page,
    new Set(["account", "account_loading", "trust", "phone", "otp", "captcha", "locked"]),
    TIMEOUT,
  );
}

async function choosePhoneText(page, phone) {
  const tail = phone.replace(/\D/g, "").slice(-2);
  const choices = await visible(page, (frame) => frame.getByRole("button"));
  let matches = [];
  for (const choice of choices) {
    const label = await controlLabel(choice);
    if (label === null) continue;
    if (label.replace(/\D/g, "").endsWith(tail)) matches.push(choice);
  }
  if (!matches.length) matches = await phoneChoiceControls(page);
  if (matches.length !== 1) throw new Error(`Phone-tail choice is not unique: ${matches.length}`);
  await matches[0].click({ timeout: STATE_TIMEOUT });
  return await waitState(
    page,
    new Set(["otp", "captcha", "locked"]),
    TIMEOUT,
  );
}

async function enterOtp(page, code) {
  await page.bringToFront();
  const inputs = await visible(page, (frame) =>
    frame.locator("input[type='tel'][autocomplete='off'], input[inputmode='numeric'], input[autocomplete='one-time-code']"));
  if (inputs.length === 1) await inputs[0].fill(code);
  else if (inputs.length === 6) {
    for (const input of inputs) await input.fill("");
    await inputs[0].focus();
    await inputs[0].pressSequentially(code);
  } else throw new Error(`verification code inputs must be one or six; found ${inputs.length}`);
}

async function trustBrowser(page) {
  const trustButtons = await activeControls(page, (frame) => frame.getByRole("button", { name: /^Trust$/i }));
  if (trustButtons.length !== 1) throw new Error("Trust control is not unique");
  await trustButtons[0].click({ timeout: STATE_TIMEOUT });
  return await waitState(page, new Set(["account", "account_loading", "closed", "captcha", "locked"]));
}

async function main() {
  let credentials = await readCredentials();
  let latest = null;
  let smsPage = null;

  try {
    await restartLocalBrowser();
    let { context, page, sessionId } = await currentApplePage();

    if (await accountIdentityMatches(page, credentials)) {
      return emitVerifiedLogin(page, sessionId, true);
    }

    let state = await waitState(
      page,
      new Set(["account", "trust", "login", "phone", "otp", "captcha", "locked"]),
      TIMEOUT,
    );
    if (["captcha", "locked"].includes(state)) throw new Error(`Apple sign-in blocked: ${state}`);
    if (state === "login") state = await completeLogin(page, credentials);
    if (["captcha", "locked"].includes(state)) throw new Error(`Apple sign-in blocked: ${state}`);

    if (state === "phone") {
      await page.bringToFront();
      state = await choosePhoneText(page, credentials.phone);
      if (state === "otp") {
        await page.waitForTimeout(10_000);
        smsPage = await openSms(context, credentials.smsUrl);
      }
    } else if (state === "otp") {
      await page.waitForTimeout(10_000);
      smsPage = await openSms(context, credentials.smsUrl);
      await page.bringToFront();
    }

    if (["captcha", "locked"].includes(state)) throw new Error(`Apple sign-in blocked: ${state}`);

    if (state === "otp") {
      latest = await waitNewestSms(smsPage, credentials.smsUrl);
      await enterOtp(page, latest.code);
      await smsPage.close();
      smsPage = null;
    }

    state = await waitState(page, new Set(["account", "account_loading", "trust", "captcha", "locked"]));
    if (state === "trust") state = await trustBrowser(page);
    if (["captcha", "locked"].includes(state)) throw new Error(`Apple sign-in blocked: ${state}`);

    if (state === "closed") {
      ({ context, page } = await approvedPage(ACCOUNT_URL));
      state = await waitState(page, new Set(["account", "account_loading", "login", "captcha", "locked"]));
    }
    if (state === "account_loading") {
      await page.goto(ACCOUNT_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT }).catch(() => {});
      state = await waitState(
        page,
        new Set(["account", "login", "captcha", "locked"]),
        TIMEOUT,
      );
    }
    if (state !== "account") throw new Error(`utm-10 did not reach the Developer Account page: ${state}`);
    if (!await waitForAccountIdentity(page, credentials)) {
      throw new Error("Displayed Apple Account email or user name does not match Notion");
    }

    credentials = null;
    latest = null;
    return emitVerifiedLogin(page, sessionId);
  } finally {
    if (smsPage && !smsPage.isClosed()) await smsPage.close().catch(() => {});
    credentials = null;
    latest = null;
  }
}

main().catch((error) => {
  process.stderr.write(`UTM_10_ERROR=${error.message}\n`, () => process.exit(1));
});
