from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_business_guest_is_playwright_only() -> None:
    main = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    assert 'import(new URL("./session.mjs"' in main
    assert "connectLocalBrowser" in main
    assert "currentSessionIdentity" in main
    assert "page.getByRole" in main
    assert "page.locator" in main
    assert 'getByRole(type, { name: text, exact: false })' in main
    assert "control.evaluate((element) => element.click())" in main
    assert 'fieldByLabel(holder, "Account Holder Name"' in main
    assert 'fieldByLabel(holder, "Account Holder Type"' in main
    assert 'fieldByLabel(countryDialog, "Bank Country or Region"' in main
    assert '"Bank Country or Region select"' in main
    assert 'await fallback.waitFor({ state: "visible", timeout: TIMEOUT })' in main
    assert "async function enabledRoleAction" in main
    assert 'await action.click({ trial: true, timeout: TIMEOUT })' in main
    assert 'enabledRoleAction(countryDialog, "Next", "bank country Next")' in main
    assert 'enabledRoleAction(holder, "Next", "Account Holder Details Next")' in main
    assert 'enabledRoleAction(details, "Next", "bank details Next")' in main
    assert 'enabledRoleAction(certification, "Add", "bank Certification Add")' in main
    assert "const bankNumbersReadback" in main
    assert "bankNumbersReadback.routingNumber !== bankInput.routingNumber" in main
    assert "bankNumbersReadback.accountNumber !== bankInput.accountNumber" in main
    assert 'throw new Error("bank page numbers do not exactly match current Notion values")' in main
    assert 'input.stage === "bank"' in main
    assert 'UTM_BUSINESS_BANK_STAGE: "verified"' in main
    assert 'hasText: "Account Holder Details"' in main
    assert "const bankFlowActive" in main
    assert "if (bankFlowActive)" in main
    assert 'existing.state !== "verified"' in main
    assert "await page.close()" in main
    assert 'reopenReason: "full-entry-replay"' in main
    assert "async function itemRowStatus" in main
    assert 'await itemRowStatus(page, "Paid Apps Agreement")' in main
    assert 'await itemRowStatus(page, "U.S. Certificate of Foreign Status of Beneficial Owner")' in main
    assert 'await itemRowStatus(page, "U.S. Form W-8BEN")' in main
    assert 'await itemRowStatus(page, "Directive on Administrative Cooperation - 7th Amendment")' in main
    assert 'status === "Active"' in main
    assert "if (!markers.length) return null" in main
    assert "for (const marker of markers)" in main
    assert 'current.tagName === "TR"' in main
    assert "directTexts.includes(value)" in main
    assert "const addBankButtons = await visible" in main
    assert 'page.getByRole("button", { name: "Add Bank Account", exact: true })' in main
    assert 'name: "Add Bank Account", exact: true' in main
    assert "if (!addBankButtons.length)" in main
    assert 'name: "Bank Accounts", exact: true' in main
    assert 'bankHeading.locator("xpath=ancestor::*[.//button[normalize-space()=\'Add Bank Account\']][1]")' in main
    assert "UTM_BUSINESS" in main
    for forbidden in (
        "utm_business_guest.py",
        "utm_business_workflow.py",
        "edge_accessibility.py",
        "Accessibility",
        "UTM_14",
        "utm-14",
        "utm-15",
    ):
        assert forbidden not in main


def test_business_otp_reuses_the_platform_agnostic_login_sms_page_flow() -> None:
    source = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    start = source.index("function extractLatestSixDigitCode")
    end = source.index("function parseBirthday", start)
    otp = source[start:end]

    assert 'normalized.matchAll(/(?<!\\d)(\\d{6})(?!\\d)/g)' in otp
    assert "return codes.at(-1) || null" in otp
    assert "async function readMessages(smsPage)" in otp
    assert 'smsPage.locator("body").textContent' in otp
    assert "async function openSmsInbox(page, smsUrl)" in otp
    assert "async function openSms(context, smsUrl)" in otp
    assert "async function waitNewestSms(smsPage, smsUrl)" in otp
    assert otp.count("await smsPage.reload") == 1
    assert "smsPage = await openSms(page.context(), smsUrl)" in otp
    assert "latest = await waitNewestSms(smsPage, smsUrl)" in otp
    assert "if (smsPage && !smsPage.isClosed()) await smsPage.close()" in otp
def test_business_otp_detection_and_phone_choice_do_not_require_english_copy() -> None:
    source = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    pending_start = source.index("async function twoFactorPending(page)")
    pending_end = source.index("async function waitForStableBusinessPage", pending_start)
    pending = source[pending_start:pending_end]
    otp_start = source.index("async function completeOtp(page)")
    otp_end = source.index("function parseBirthday", otp_start)
    otp = source[otp_start:otp_end]

    assert "visibleAcrossFrames" in pending
    assert "inputmode='numeric'" in pending
    assert "phoneTail" in pending
    assert 'frame.getByRole("button")' in pending
    assert 'frame.getByRole("button")' in otp
    assert "name: /text message/i" not in source


def test_business_guest_reads_all_inputs_from_ssh_stdin() -> None:
    source = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    for required in (
        "CONTEXT_ID",
        "VM_NAME",
        "APP_NAME",
        "BUNDLE_ID",
        "BIRTHDAY",
        "APPLE_ACCOUNT_PHONE",
        "APPLE_ACCOUNT_SMS_URL",
        "ABA_ROUTING_NUMBER",
        "ACCOUNT_NUMBER",
        "EXPECTED_EDGE_PID",
        "EXPECTED_EDGE_WEBSOCKET",
    ):
        assert required in source
    assert "readNotionBankInput" not in source
    assert "resolve-app" not in source


def test_dsa_custom_radio_uses_the_exact_text_row_even_when_native_input_is_hidden() -> None:
    source = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    control_start = source.index("async function controlByText(page, text, type = \"checkbox\")")
    control_end = source.index("async function setControl", control_start)
    control = source[control_start:control_end]
    setter_start = control_end
    setter_end = source.index("async function fieldByLabel", setter_start)
    setter = source[setter_start:setter_end]

    assert "const controls = container.locator(`input[type='${type}']`);" in control
    assert "if (await controls.count() === 1)" in control
    assert "matches.push(controls.first())" in control
    assert "if (await control.isVisible().catch(() => false))" in setter
    assert "else await control.evaluate((element) => element.click())" in setter


def test_dsa_waits_for_the_non_trader_radio_after_the_dialog_shell_appears() -> None:
    source = (ROOT / "scripts/utm_business_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function completeDsa(page, sessionId, run)")
    end = source.index("async function confirmStandardizedLegalEntityAddress", start)
    dsa = source[start:end]
    answer = dsa.index('const answer = "I\'m not a trader under the DSA')
    readiness = dsa.index("await waitUntil(page, async () => {", answer)
    locate = dsa.index('await controlByText(page, answer, "radio")', readiness)
    select = dsa.index('await setControl(page, answer, "radio")', locate)
    assert answer < readiness < locate < select
