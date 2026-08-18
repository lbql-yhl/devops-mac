import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import utm_apps


ROOT = Path(__file__).resolve().parents[1]


def test_delivery_script_runs_from_its_absolute_entry() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "utm_apps_delivery.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr


def test_utm10_plain_text_sms_parser_self_test_runs_without_browser() -> None:
    completed = subprocess.run(
        ["/usr/bin/env", "node", str(ROOT / "scripts" / "utm_10_login.mjs"), "--self-test"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "UTM_10_ONE_SCRIPT=self-test-passed"


def _run_utm10_parser_fixture(message: str) -> subprocess.CompletedProcess[str]:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    start = source.index("function extractLatestSixDigitCode(")
    end = source.index("\n}\n", start) + 2
    parser_source = source[start:end]
    fixture = json.dumps(message)
    return subprocess.run(
        [
            "/usr/bin/env",
            "node",
            "--input-type=module",
            "--eval",
            f"{parser_source}\nconsole.log(extractLatestSixDigitCode({fixture}) ?? '');",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )


def test_utm10_sms_parser_accepts_six_digits_next_to_non_digit_text() -> None:
    completed = _run_utm10_parser_fixture(
        "yes|verification_code_123456_received"
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "123456"


def test_utm10_sms_parser_uses_the_last_six_digit_code_without_dates_or_copy_rules() -> None:
    completed = _run_utm10_parser_fixture(
        "yes|old_112233_received\nokay|new_654321_received"
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "654321"


def test_utm10_sms_parser_ignores_no_status_even_when_other_numbers_are_present() -> None:
    completed = _run_utm10_parser_fixture(
        "no|20260814暂时未有邮件，请试试重发吧，记录123456"
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


def test_utm10_sms_reader_waits_for_dynamic_plain_text_content() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    parser_start = source.index("function extractLatestSixDigitCode(")
    parser_end = source.index("\n}\n", parser_start) + 2
    reader_start = source.index("async function readMessages(")
    reader_end = source.index("\n}\n", reader_start) + 2
    script = "\n".join(
        [
            "const STATE_TIMEOUT = 100;",
            source[parser_start:parser_end],
            source[reader_start:reader_end],
            "let reads = 0;",
            "const bodies = ['', 'ok|code_654321_received'];",
            "const smsPage = {",
            "  locator: () => ({ textContent: async () => bodies[Math.min(reads++, bodies.length - 1)] }),",
            "  waitForTimeout: async () => {},",
            "};",
            "const messages = await readMessages(smsPage);",
            "console.log(JSON.stringify({ messages, reads }));",
        ]
    )
    completed = subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "messages": [{"code": "654321"}],
        "reads": 2,
    }


def test_utm10_no_status_waits_then_refreshes_once_before_reading_yes_status() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")

    def function_source(name: str) -> str:
        start = source.index(name)
        return source[start : source.index("\n}\n", start) + 2]

    script = "\n".join(
        [
            "const STATE_TIMEOUT = 5;",
            "const TIMEOUT = 100;",
            function_source("function extractLatestSixDigitCode("),
            function_source("async function readMessages("),
            "let inboxOpens = 0;",
            "async function openSmsInbox() { inboxOpens += 1; }",
            function_source("async function waitNewestSms("),
            "let reloaded = false;",
            "let reloads = 0;",
            "const smsPage = {",
            "  locator: () => ({ textContent: async () => reloaded",
            "    ? '验证码：654321。'",
            "    : 'no|20260814暂时未有邮件，请试试重发吧，记录123456' }),",
            "  waitForTimeout: async () => new Promise((resolve) => setTimeout(resolve, 1)),",
            "  reload: async () => { reloaded = true; reloads += 1; },",
            "};",
            "const latest = await waitNewestSms(smsPage, 'https://sms.example.test/?token=fake');",
            "console.log(JSON.stringify({ latest, reloads, inboxOpens }));",
        ]
    )
    completed = subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "latest": {"code": "654321"},
        "reloads": 1,
        "inboxOpens": 1,
    }


def test_preflight_checks_all_playwright_files() -> None:
    command = utm_apps.guest_preflight_command("abcd")
    for filename in utm_apps.GUEST_WRAPPER_FILENAMES:
        assert f"/Users/example/Downloads/AppleAccountScriptsBackup/{filename}" in command
    assert "playwright-core/index.mjs" in command
    assert "node --check" in command


def test_preflight_sha256_matches_every_current_host_playwright_file() -> None:
    command = utm_apps.guest_preflight_command("abcd")

    assert command.count("/usr/bin/shasum -a 256") == len(
        utm_apps.GUEST_WRAPPER_FILENAMES
    )
    for filename in utm_apps.GUEST_WRAPPER_FILENAMES:
        expected = hashlib.sha256(
            (ROOT / "scripts" / filename).read_bytes()
        ).hexdigest()
        assert expected in command


def test_stage_execution_uses_node_for_all_four_stages() -> None:
    for stage, filename in utm_apps.GUEST_WRAPPERS.items():
        command = utm_apps.guest_execution_command("abcd", stage)
        assert f"/Users/example/Downloads/AppleAccountScriptsBackup/{filename}" in command
        assert "node" in command
        assert "currentSessionIdentity" in command


def test_utm12_stage_execution_uses_180_second_guest_timeout() -> None:
    command = utm_apps.guest_execution_command("igec", "utm-12")
    assert "/usr/bin/pkill -f" in command
    assert "^node /Users/example/Downloads/AppleAccountScriptsBackup/utm_12_one\\.mjs$" in command
    assert "/usr/bin/perl -e" in command
    assert "alarm shift; exec @ARGV" in command
    assert "UTM_12_ERROR=TIMEOUT_180_SECONDS" in command


@pytest.mark.parametrize("stage", ("utm-11", "utm-13"))
def test_non_utm12_stage_execution_keeps_60_second_guest_timeout(stage: str) -> None:
    command = utm_apps.guest_execution_command("igec", stage)

    stage_code = stage.upper().replace("-", "_")
    assert f"{stage_code}_ERROR=TIMEOUT_60_SECONDS" in command
    assert f"{stage_code}_ERROR=TIMEOUT_180_SECONDS" not in command


def test_utm10_guest_stage_uses_180_second_timeout_with_transport_grace() -> None:
    observed = {}

    def runner(command, **kwargs):
        observed["command"] = command[-1]
        observed["timeout"] = kwargs["timeout"]
        raise RuntimeError("stop after capturing utm-10 timeouts")

    with pytest.raises(RuntimeError, match="stop after capturing utm-10 timeouts"):
        utm_apps.run_guest_stage(
            stage="utm-10",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={
                "APPLE_ACCOUNT_EMAIL": "mail@example.test",
                "APPLE_ACCOUNT_PASSWORD": "secret",
                "APPLE_ACCOUNT_PHONE": "+15550100",
                "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/",
                "APPLE_ACCOUNT_USER_NAME": "Marius Engel",
            },
            runner=runner,
            password_env={},
        )

    assert "UTM_10_ERROR=TIMEOUT_180_SECONDS" in observed["command"]
    assert observed["timeout"] == 185


def test_utm13_authorization_uses_the_runtime_guest_value(monkeypatch) -> None:
    dummy_value = "9072"
    monkeypatch.setattr(utm_apps, "guest_password", lambda: dummy_value)
    payload = json.loads(
        utm_apps._stage_stdin(
            "utm-13",
            {"RUN_ID": "run-1", "APPLE_ACCOUNT_USER_NAME": "Marius Engel"},
        )
    )
    assert payload["authorizationAttemptId"] == "current-run"
    assert payload["certificateUserName"] == "Marius Engel"
    assert payload["systemKeychainPassword"] == dummy_value
    assert "FIXED_PASSWORD" not in (ROOT / "scripts" / "utm_apps.py").read_text(
        encoding="utf-8"
    )


def test_utm12_identifier_recovery_checks_unique_app_name_before_create() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function recoverIdentifier(page, config)")
    end = source.index("async function registerIdentifierFromForm", start)
    recover = source[start:end]
    name_check = recover.index("exactIdentifierNameMatches(page, config.appName)")
    bundle_check = recover.index("exactBundleMatches(page, config.bundleId)")
    assert name_check < bundle_check
    assert "multiple exact App ID name matches found" in recover
    assert "if (nameMatches.length === 1) return true;" in recover


def test_utm13_certificate_recovery_checks_notion_user_name_before_any_download() -> None:
    source = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    candidates_start = source.index("async function certificateCandidates(")
    candidates_end = source.index("async function clickRow", candidates_start)
    candidates = source[candidates_start:candidates_end]
    assert "expectedUserName" in candidates
    assert "normalizedUserName" in candidates
    assert "return exactName;" in candidates

    ensure_start = source.index("async function ensureDistributionCertificate(")
    ensure_end = source.index("async function classifyProfileRows", ensure_start)
    ensure = source[ensure_start:ensure_end]
    candidate_check = ensure.index("certificateCandidates(page, handoff.userName)")
    recovery = ensure.index("recoverCertificateDownload(")
    create = ensure.index("createCertificate(page, sessionId, handoff, csr)")
    assert candidate_check < recovery < create
    existing = ensure.index("if (candidates.length >= 1)", candidate_check)
    assert existing < recovery
    assert "existing: true" in ensure[existing:recovery]


def test_utm13_existing_distribution_certificate_match_does_not_depend_on_date_format() -> None:
    source = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function certificateCandidates(")
    end = source.index("async function clickRow", start)
    candidates = source[start:end]
    assert "dateLooksCurrent" not in candidates
    assert "const exactName = typed.filter" in candidates


def test_utm13_any_existing_distribution_certificate_for_notion_user_skips_creation() -> None:
    source = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    candidates_start = source.index("async function certificateCandidates(")
    candidates_end = source.index("async function clickRow", candidates_start)
    candidates = source[candidates_start:candidates_end]
    assert "exactName.length > 1" not in candidates

    ensure_start = source.index("async function ensureDistributionCertificate(")
    ensure_end = source.index("async function classifyProfileRows", ensure_start)
    ensure = source[ensure_start:ensure_end]
    assert "if (candidates.length >= 1)" in ensure
    existing = ensure.index("if (candidates.length >= 1)")
    create = ensure.index("createCertificate(page, sessionId, handoff, csr)")
    assert existing < create


def test_utm13_skips_certificate_creation_when_one_active_distribution_certificate_exists() -> None:
    source = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureDistributionCertificate(")
    end = source.index("async function classifyProfileRows", start)
    ensure = source[start:end]
    existing = ensure.index("if (candidates.length >= 1)")
    create = ensure.index("createCertificate(page, sessionId, handoff, csr)")
    assert existing < create
    existing_return = ensure.index("};", existing) + 2
    assert "existing: true" in ensure[existing:existing_return]
    assert "downloadCertificate" not in ensure[existing:existing_return]
    assert "importCertificate" not in ensure[existing:existing_return]


def test_utm_apps_clean_stage_output(capsys) -> None:
    utm_apps._print_stage_start("utm-10")
    utm_apps._print_stage_result("utm-10", skipped_existing=True)
    utm_apps._print_stage_start("utm-13")
    utm_apps._print_stage_result("utm-13", skipped_existing=False)
    assert capsys.readouterr().out.splitlines() == [
        "开始 UTM-10 登陆账号步骤",
        "UTM-10 登陆账号步骤检查已存在，跳过",
        "开始 UTM-13证书步骤和Profile步骤",
        "UTM-13证书步骤和Profile步骤已完成",
    ]


def test_guest_results_expose_existing_step_evidence_for_clean_output() -> None:
    utm10 = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    utm11 = (ROOT / "scripts" / "utm_11_one.mjs").read_text(encoding="utf-8")
    utm12 = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    utm13 = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    assert "EXISTING_ACCOUNT" in utm10
    assert "EXISTING_BUSINESS" in utm11
    assert "EXISTING_IDENTIFIER" in utm12
    assert "EXISTING_APP" in utm12
    assert "EXISTING_CERTIFICATE" in utm13
    assert "EXISTING_PROFILE" in utm13


def test_utm13_skips_profile_creation_when_exact_profile_row_exists() -> None:
    source = (ROOT / "scripts" / "utm_13_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureProvisioningProfile(")
    end = source.index("async function runUtm13()", start)
    ensure = source[start:end]
    existing = ensure.index("if (matches.length === 1)")
    create = ensure.index("createProfile(page, sessionId, handoff, identity)", existing)
    assert existing < create
    assert 'return "existing-profile";' in ensure[existing:create]
    assert "verifyProfileRow(matches[0], handoff)" in ensure[existing:create]


def test_session_has_a_read_only_page_inspection_command() -> None:
    source = (ROOT / "scripts" / "session.mjs").read_text(encoding="utf-8")
    assert "async function inspectPages()" in source
    assert 'command === "inspect"' in source
    assert "body_text" in source
    assert "links:" in source
    assert "if (!approved) continue;" in source
    start = source.index("async function inspectPages()")
    end = source.index("async function stop()", start)
    assert "() => process.exit(0)" in source[start:end]


def test_approved_page_reuses_the_single_edge_startup_tab() -> None:
    source = (ROOT / "scripts" / "session.mjs").read_text(encoding="utf-8")
    start = source.index("export async function approvedPage")
    end = source.index("export async function exactlyOne", start)
    approved_page = source[start:end]
    assert "const reusable = context.pages().length === 1 ? context.pages()[0] : null;" in approved_page
    assert "const page = existing || reusable || await context.newPage();" in approved_page
    assert 'page.goto(approvedUrl, { waitUntil: "domcontentloaded", timeout: 30_000 })' in approved_page


def test_utm10_page_and_post_navigation_state_waits_allow_30_seconds() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    assert "async function waitState(page, accepted, timeout = TIMEOUT)" in source
    assert "const deadline = Date.now() + TIMEOUT;" in source[source.index("async function loginAction"):source.index("async function pageState")]
    assert "const passwordDeadline = Date.now() + TIMEOUT;" in source


def test_utm11_new_page_open_wait_allows_30_seconds() -> None:
    source = (ROOT / "scripts" / "utm_11_one.mjs").read_text(encoding="utf-8")
    assert 'context.waitForEvent("page", { timeout: TIMEOUT })' in source


def test_utm12_spa_filter_readiness_allows_30_seconds() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function filterListByExactValue(")
    end = source.index("async function clickUniqueContinue", start)
    helper = source[start:end]
    assert 'waitForLoadState("networkidle", { timeout: TIMEOUT })' in helper
    assert 'waitFor({ state: "hidden", timeout: TIMEOUT })' in helper


def _run_utm12_app_store_state_fixture(
    states: list[dict[str, object]],
    command: str,
) -> subprocess.CompletedProcess[str]:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")

    def function_source(signature: str) -> str:
        start = source.index(signature)
        return source[start : source.index("\n}\n", start) + 2]

    helper_sources = []
    controls_signature = "async function appStoreAgreementControls(page)"
    if controls_signature in source:
        helper_sources.append(function_source("async function checkboxStatement(checkbox)"))
        helper_sources.append(function_source(controls_signature))
    helper_sources.append(function_source("async function appStoreAgreementPresent(page)"))

    apps_ready_signature = "async function appStoreAppsReady(page)"
    if apps_ready_signature in source:
        helper_sources.append(function_source(apps_ready_signature))
    else:
        helper_sources.append(
            """
async function appStoreAppsReady(page) {
  const url = new URL(page.url());
  if (url.hostname !== "appstoreconnect.apple.com"
      || url.searchParams.has("rpf")
      || !(url.pathname === "/apps" || url.pathname.startsWith("/apps/"))) return false;
  const state = page.__state();
  if (state.terms || state.checkboxes || state.agrees) return false;
  return Boolean(state.headings || state.searches || state.addApps
    || state.distributionLinks || state.versions);
}
"""
        )

    wait_signature = "async function waitForAppStoreState(page)"
    if wait_signature in source:
        helper_sources.append(function_source(wait_signature))
    else:
        helper_sources.append(
            """
async function waitForAppStoreState(page) {
  const deadline = Date.now() + TIMEOUT;
  while (true) {
    if (await appStoreAgreementPresent(page)) return "agreement";
    if (await appStoreAppsReady(page)) return "apps";
    if (Date.now() >= deadline) return "unknown";
    await page.waitForTimeout(1);
  }
}
"""
        )

    fixture = json.dumps({"states": states, "command": command})
    script = "\n".join(
        [
            "const TIMEOUT = 20;",
            f"const fixture = {fixture};",
            "let index = 0;",
            "let polls = 0;",
            "const current = () => fixture.states[Math.min(index, fixture.states.length - 1)];",
            "function elements(count, enabled = true) {",
            "  return Array.from({ length: Number(count || 0) }, () => ({",
            "    isVisible: async () => true,",
            "    isEnabled: async () => enabled,",
            "  }));",
            "}",
            "function checkboxElements(state) {",
            "  const statements = Array.isArray(state.checkboxStatements)",
            "    ? state.checkboxStatements",
            "    : Array.from({ length: Number(state.checkboxes || 0) }, () => '');",
            "  return statements.map((statement, testId) => ({",
            "    testId,",
            "    isVisible: async () => true,",
            "    isEnabled: async () => true,",
            "    evaluate: async () => statement,",
            "  }));",
            "}",
            "function locator(items) {",
            "  return {",
            "    count: async () => items.length,",
            "    nth: (itemIndex) => items[itemIndex],",
            "  };",
            "}",
            "const frame = {",
            "  getByText: (value) => {",
            "    const state = current();",
            "    if (value === 'Terms Of Service') return locator(elements(state.terms));",
            "    if (value instanceof RegExp && value.test('iOS App Version 1.0'))",
            "      return locator(elements(state.versions));",
            "    return locator([]);",
            "  },",
            "  getByRole: (role, options = {}) => {",
            "    const state = current();",
            "    if (role === 'dialog') return locator(elements(state.dialogs));",
            "    if (role === 'heading') return locator(elements(state.headings));",
            "    if (role === 'button' && options.name === 'Agree')",
            "      return locator(elements(state.agrees, state.agreeEnabled !== false));",
            "    if (role === 'button' && (options.name === 'Add Apps' || options.name === 'New App'))",
            "      return locator(elements(state.addApps));",
            "    if (role === 'button' && options.name instanceof RegExp",
            "        && options.name.test('Agree'))",
            "      return locator(elements(state.agrees, state.agreeEnabled !== false));",
            "    return locator([]);",
            "  },",
            "  locator: (selector) => {",
            "    const state = current();",
            "    if (selector.includes('checkbox')) return locator(checkboxElements(state));",
            "    if (selector.includes('search')) return locator(elements(state.searches));",
            "    if (selector.includes('distribution')) return locator(elements(state.distributionLinks));",
            "    return locator([]);",
            "  },",
            "};",
            "const page = {",
            "  frames: () => [frame],",
            "  url: () => current().url,",
            "  __state: current,",
            "  waitForTimeout: async () => { polls += 1; index = Math.min(index + 1, fixture.states.length - 1); },",
            "};",
            function_source("async function visible(locator)"),
            function_source("async function visibleInFrames(page, locatorFactory)"),
            function_source("async function agreementNeeded(page)"),
            *helper_sources,
            "async function waitForSafePage() {}",
            function_source("async function appStoreAgreementComplete(page)"),
            "try {",
            "  let result;",
            "  if (fixture.command === 'present') result = await appStoreAgreementPresent(page);",
            "  else if (fixture.command === 'complete') result = await appStoreAgreementComplete(page);",
            "  else if (fixture.command === 'controls') {",
            "    const controls = await appStoreAgreementControls(page);",
            "    result = { checkboxIds: controls.checkboxes.map((item) => item.testId) };",
            "  }",
            "  else result = await waitForAppStoreState(page);",
            "  console.log(JSON.stringify({ ok: true, result, index, polls }));",
            "} catch (error) {",
            "  console.log(JSON.stringify({ ok: false, error: error.message, index, polls }));",
            "}",
        ]
    )
    return subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )


def test_utm12_waits_for_delayed_non_dialog_tos_on_rpf_route() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {"url": "https://appstoreconnect.apple.com/apps"},
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 1,
            },
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 1,
                "checkboxes": 1,
                "agrees": 1,
                "agreeEnabled": False,
            },
        ],
        "wait",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "result": "agreement",
        "index": 2,
        "polls": 2,
    }


def test_utm12_tos_presence_does_not_require_dialog_role() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 1,
                "checkboxes": 1,
                "agrees": 1,
                "agreeEnabled": False,
                "dialogs": 0,
            }
        ],
        "present",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is True


def test_utm12_apps_completion_requires_positive_ui_evidence() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [{"url": "https://appstoreconnect.apple.com/apps"}],
        "complete",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is False


def test_utm12_partial_tos_blocks_background_apps_heading() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {
                "url": "https://appstoreconnect.apple.com/apps",
                "terms": 1,
                "headings": 1,
            }
        ],
        "complete",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is False


def test_utm12_apps_completion_accepts_real_apps_heading() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [{"url": "https://appstoreconnect.apple.com/apps", "headings": 1}],
        "complete",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is True


def test_utm12_tos_controls_must_be_unique() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 2,
                "checkboxes": 1,
                "agrees": 1,
            }
        ],
        "present",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["error"] == (
        "App Store Connect Terms Of Service must be uniquely visible; found 2"
    )


def test_utm12_selects_the_tos_checkbox_from_four_visible_checkboxes() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 1,
                "checkboxStatements": [
                    "Send reports",
                    "Use analytics",
                    "I have read and agree to the terms presented above.",
                    "Email updates",
                ],
                "agrees": 1,
            }
        ],
        "controls",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "result": {"checkboxIds": [2]},
        "index": 0,
        "polls": 0,
    }


def test_utm12_rejects_multiple_tos_statement_checkboxes() -> None:
    completed = _run_utm12_app_store_state_fixture(
        [
            {
                "url": "https://appstoreconnect.apple.com/?rpf=%2Fapps",
                "terms": 1,
                "checkboxStatements": [
                    "I have read and agree to the terms presented above.",
                    "Use analytics",
                    "I agree to the terms.",
                    "Email updates",
                ],
                "agrees": 1,
            }
        ],
        "controls",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["error"] == (
        "App Store Connect agreement checkbox must be uniquely visible; found 2"
    )


def _run_utm12_prepare_agreement_checkbox_fixture(
    *,
    initially_enabled: bool,
    scrollable_count: int,
    enable_after_polls: int | None,
) -> subprocess.CompletedProcess[str]:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function prepareAppStoreAgreementCheckbox(page, checkbox)")
    helper = source[start : source.index("\n}\n", start) + 2]
    fixture = json.dumps(
        {
            "initiallyEnabled": initially_enabled,
            "scrollableCount": scrollable_count,
            "enableAfterPolls": enable_after_polls,
        }
    )
    script = "\n".join(
        [
            "const TIMEOUT = 20;",
            f"const fixture = {fixture};",
            "let enabled = fixture.initiallyEnabled;",
            "let polls = 0;",
            "let evaluateCalls = 0;",
            "const checkbox = {",
            "  isEnabled: async () => enabled,",
            "  evaluate: async () => { evaluateCalls += 1; return fixture.scrollableCount; },",
            "};",
            "const page = {",
            "  waitForTimeout: async () => {",
            "    polls += 1;",
            "    if (fixture.enableAfterPolls !== null && polls >= fixture.enableAfterPolls) enabled = true;",
            "  },",
            "};",
            helper,
            "try {",
            "  await prepareAppStoreAgreementCheckbox(page, checkbox);",
            "  console.log(JSON.stringify({ ok: true, polls, evaluateCalls }));",
            "} catch (error) {",
            "  console.log(JSON.stringify({ ok: false, error: error.message, polls, evaluateCalls }));",
            "}",
        ]
    )
    return subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )


def test_utm12_scrolls_the_unique_tos_container_and_waits_for_checkbox_enablement() -> None:
    completed = _run_utm12_prepare_agreement_checkbox_fixture(
        initially_enabled=False,
        scrollable_count=1,
        enable_after_polls=2,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "polls": 2,
        "evaluateCalls": 1,
    }


def test_utm12_scrolls_tos_even_when_checkbox_reports_enabled_before_scroll() -> None:
    completed = _run_utm12_prepare_agreement_checkbox_fixture(
        initially_enabled=True,
        scrollable_count=1,
        enable_after_polls=None,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "polls": 0,
        "evaluateCalls": 1,
    }


def test_utm12_rejects_ambiguous_tos_scroll_containers() -> None:
    completed = _run_utm12_prepare_agreement_checkbox_fixture(
        initially_enabled=False,
        scrollable_count=2,
        enable_after_polls=None,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": False,
        "error": "App Store Connect agreement scroll container must be unique; found 2",
        "polls": 0,
        "evaluateCalls": 1,
    }


def test_utm12_prepares_disabled_tos_checkbox_before_checking_it() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    helper_start = source.index("async function prepareAppStoreAgreementCheckbox(page, checkbox)")
    helper_end = source.index("\n}\n", helper_start) + 2
    helper = source[helper_start:helper_end]
    click_start = source.index("async function clickAppStoreAgreementCheckbox(page, checkbox)")
    click_end = source.index("\n}\n", click_start) + 2
    click_helper = source[click_start:click_end]
    ensure_start = source.index("async function ensureAppStoreAgreement(page, run)")
    ensure_end = source.index("async function developerAgreementPresent(page)", ensure_start)
    ensure = source[ensure_start:ensure_end]

    assert "scrollHeight > candidate.clientHeight" in helper
    assert "candidate.scrollTop = candidate.scrollHeight" in helper
    assert "await checkbox.isEnabled()" in helper
    assert ensure.index("await prepareAppStoreAgreementCheckbox(page, checkbox)") < ensure.index(
        "await clickAppStoreAgreementCheckbox(page, checkbox)"
    )
    assert 'checkbox.locator("xpath=ancestor::label[1]")' in click_helper
    assert 'label.locator("div[type=\'checkbox\']")' in click_helper
    assert "App Store Connect visual agreement checkbox must be unique" in click_helper
    assert "await targets[0].click({ timeout: SHORT_TIMEOUT })" in click_helper
    assert "await checkbox.isChecked()" in click_helper
    assert "checkbox.check({ force: true" not in ensure


def test_utm12_agreement_identity_uses_unique_legal_scroll_text_not_whole_page() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    identity_start = source.index("async function appStoreAgreementDocumentText(checkbox)")
    identity_end = source.index("\n}\n", identity_start) + 2
    identity = source[identity_start:identity_end]
    ensure_start = source.index("async function ensureAppStoreAgreement(page, run)")
    ensure_end = source.index("async function developerAgreementPresent(page)", ensure_start)
    ensure = source[ensure_start:ensure_end]

    assert "candidate.scrollHeight > candidate.clientHeight" in identity
    assert "/terms\\s+of\\s+service/i" in identity
    assert "agreement document must be unique" in identity
    assert "await appStoreAgreementDocumentText(checkbox)" in ensure
    assert "await appStoreAgreementDocumentText(controls.checkboxes[0])" in ensure
    assert 'page.locator("body").innerText()' not in ensure


def test_utm12_only_migrates_a_planned_app_agreement_ledger_input() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    prepare_start = source.index("async function prepareLedger(")
    prepare_end = source.index("async function updateLedger", prepare_start)
    prepare = source[prepare_start:prepare_end]
    ensure_start = source.index("async function ensureAppStoreAgreement(page, run)")
    ensure_end = source.index("async function developerAgreementPresent(page)", ensure_start)
    ensure = source[ensure_start:ensure_end]

    assert "allowPlannedInputMigration = false" in prepare
    assert 'existing.state === "planned"' in prepare
    assert "existing.sessionId === run.sessionId" in prepare
    assert "previousInputSummaryHash" in prepare
    assert "allowPlannedInputMigration: true" in ensure


def test_utm12_uses_explicit_no_apps_as_safe_recreate_evidence() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    helper_start = source.index("async function appStoreNoApps(page)")
    helper_end = source.index("\n}\n", helper_start) + 2
    helper = source[helper_start:helper_end]
    ensure_start = source.index("async function ensureApp(page, run, config)")
    ensure_end = source.index("// Stage 4:", ensure_start)
    ensure = source[ensure_start:ensure_end]

    assert 'frame.getByText("No Apps", { exact: true })' in helper
    assert 'frame.getByRole("button", { name: "Add Apps", exact: true })' in helper
    reset = ensure.index("if (await appStoreNoApps(page))")
    refusal = ensure.index("read-only recovery found no exact app")
    create_dialog = ensure.index("await openNewAppDialog(page)")
    assert reset < refusal < create_dialog
    assert 'state: "planned"' in ensure[reset:refusal]
    assert 'recoveryEvidence: "verified-no-apps"' in ensure[reset:refusal]


def test_utm12_does_not_reset_unknown_creation_without_no_apps_evidence() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    ensure_start = source.index("async function ensureApp(page, run, config)")
    ensure_end = source.index("// Stage 4:", ensure_start)
    ensure = source[ensure_start:ensure_end]

    recovery = ensure.index("if (existingLedger && existingLedger.state !== \"planned\")")
    no_apps = ensure.index("if (await appStoreNoApps(page))", recovery)
    refusal = ensure.index("read-only recovery found no exact app", no_apps)
    assert recovery < no_apps < refusal


def _run_utm12_unique_visible_wait_fixture(
    visible_counts: list[int],
) -> subprocess.CompletedProcess[str]:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    visible_start = source.index("async function visible(locator)")
    visible_end = source.index("async function exactlyOne", visible_start)
    wait_start = source.index("async function waitForUniqueVisibleInFrames")
    wait_end = source.index("\n}\n", wait_start) + 2
    helper_source = source[visible_start:visible_end] + source[wait_start:wait_end]
    fixture = json.dumps(visible_counts)
    script = "\n".join(
        [
            "const TIMEOUT = 100;",
            helper_source,
            f"const visibleCounts = {fixture};",
            "let polls = 0;",
            "let waits = 0;",
            "const targets = [{ id: 1 }, { id: 2 }];",
            "const page = {",
            "  frames: () => [{}],",
            "  waitForTimeout: async () => { waits += 1; },",
            "};",
            "const locatorFactory = () => {",
            "  const count = visibleCounts[Math.min(polls, visibleCounts.length - 1)];",
            "  polls += 1;",
            "  return {",
            "    count: async () => count,",
            "    nth: (index) => ({",
            "      isVisible: async () => true,",
            "      target: targets[index],",
            "    }),",
            "  };",
            "};",
            "try {",
            "  const result = await waitForUniqueVisibleInFrames(",
            "    page, locatorFactory, 'App ID Description',",
            "  );",
            "  console.log(JSON.stringify({ ok: true, polls, waits, id: result.target.id }));",
            "} catch (error) {",
            "  console.log(JSON.stringify({ ok: false, polls, waits, error: error.message }));",
            "}",
        ]
    )
    return subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )


def test_utm12_waits_until_description_is_uniquely_visible() -> None:
    completed = _run_utm12_unique_visible_wait_fixture([0, 0, 1])

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "polls": 3,
        "waits": 2,
        "id": 1,
    }


def test_utm12_unique_visible_wait_rejects_multiple_matches_immediately() -> None:
    completed = _run_utm12_unique_visible_wait_fixture([2])

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": False,
        "polls": 1,
        "waits": 0,
        "error": "App ID Description must be uniquely visible; found 2",
    }


def test_utm12_notion_fields_command_uses_read_only_recovery_mode() -> None:
    command = utm_apps.guest_utm12_notion_fields_command("abcd")
    assert "/Users/example/Downloads/AppleAccountScriptsBackup/utm_12_one.mjs" in command
    assert "notion-fields" in command
    assert "currentSessionIdentity" in command


def test_utm12_version_1_0_is_sufficient_creation_evidence() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function verifyAppDetail")
    end = source.index("async function recoverApp", start)
    verifier = source[start:end]
    assert "/iOS(?: App)?(?: Version)?\\s+1\\.0\\b/.test(body)" in verifier
    assert "return detailPath && versionPresent;" in verifier
    assert "version.length" not in verifier
    assert "name.length" not in verifier
    assert "distribution.length" not in verifier


def test_utm12_waits_conditionally_for_version_1_0_after_navigation() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "const APP_DETAIL_TIMEOUT = 30_000;" in source
    assert "async function waitForVerifiedAppDetail(page, config)" in source
    assert "const deadline = Date.now() + APP_DETAIL_TIMEOUT;" in source
    assert "while (Date.now() < deadline)" in source
    assert "await page.waitForTimeout(20);" in source
    assert "if (!await waitForVerifiedAppDetail(app, null))" in source


def test_utm12_waits_for_the_spa_to_enter_the_created_app_detail_route() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")

    def function_source(signature: str) -> str:
        start = source.index(signature)
        return source[start : source.index("\n}\n", start) + 2]

    script = "\n".join(
        [
            "const APP_DETAIL_TIMEOUT = 100;",
            "const states = [",
            "  { url: 'https://appstoreconnect.apple.com/apps', body: 'Apps No Apps' },",
            "  { url: 'https://appstoreconnect.apple.com/apps/123456/distribution', body: 'iOS App 1.0' },",
            "];",
            "let index = 0;",
            "let polls = 0;",
            "async function waitForSafePage() {}",
            "const page = {",
            "  url: () => states[index].url,",
            "  frames: () => [{ locator: () => ({ innerText: async () => states[index].body }) }],",
            "  waitForTimeout: async () => { polls += 1; index = Math.min(index + 1, states.length - 1); },",
            "};",
            function_source("async function verifyAppDetail(page, _config)"),
            function_source("async function waitForVerifiedAppDetail(page, config)"),
            "const result = await waitForVerifiedAppDetail(page, null);",
            "console.log(JSON.stringify({ result, index, polls }));",
        ]
    )
    completed = subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"result": True, "index": 1, "polls": 1}


def test_utm12_detail_wait_returns_false_without_polling_on_developer_account() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")

    def function_source(signature: str) -> str:
        start = source.index(signature)
        return source[start : source.index("\n}\n", start) + 2]

    script = "\n".join(
        [
            "const APP_DETAIL_TIMEOUT = 100;",
            "let polls = 0;",
            "async function waitForSafePage(_page, hostname) {",
            "  if (hostname !== 'developer.apple.com') throw new Error('Unexpected page');",
            "}",
            "const page = {",
            "  url: () => 'https://developer.apple.com/account',",
            "  frames: () => [{ locator: () => ({ innerText: async () => 'Account' }) }],",
            "  waitForTimeout: async () => { polls += 1; },",
            "};",
            function_source("async function verifyAppDetail(page, _config)"),
            function_source("async function waitForVerifiedAppDetail(page, config)"),
            "try {",
            "  const result = await waitForVerifiedAppDetail(page, null);",
            "  console.log(JSON.stringify({ ok: true, result, polls }));",
            "} catch (error) {",
            "  console.log(JSON.stringify({ ok: false, error: error.message, polls }));",
            "}",
        ]
    )
    completed = subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"ok": True, "result": False, "polls": 0}


def test_utm12_clicks_only_the_unique_create_button_inside_new_app_dialog() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]

    assert (
        'frame.getByRole("dialog").getByRole("button", { name: "Create", exact: true })'
        in ensure
    )
    assert 'frame.getByRole("button", { name: "Create", exact: true })' not in ensure


def test_utm12_gives_create_up_to_three_seconds_to_start_its_spa_transition() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "const CREATE_TRANSITION_TIMEOUT = 3_000;" in source
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    click = ensure.index("await create.click")
    grace = ensure.index("await waitForCreateTransition(page, create)", click)
    reopen = ensure.index("await recoverApp(page, config)", grace)
    assert click < grace < reopen


def test_utm12_waits_two_seconds_immediately_before_clicking_create() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    live_session = ensure.index("await assertLiveSession(run)")
    ready_wait = ensure.index("await page.waitForTimeout(2_000)", live_session)
    click = ensure.index("await create.click", ready_wait)
    assert live_session < ready_wait < click


def test_utm12_stops_immediately_on_the_real_app_name_already_used_error() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    helper_start = source.index("async function appNameAlreadyUsedError(page)")
    helper_end = source.index("\n}\n", helper_start) + 2
    helper = source[helper_start:helper_end]
    assert "The app name you entered is already being used" in helper

    transition_start = source.index("async function waitForCreateTransition(page, create)")
    transition_end = source.index("\n}\n", transition_start) + 2
    transition = source[transition_start:transition_end]
    assert "await appNameAlreadyUsedError(page)" in transition

    ensure_start = source.index("async function ensureApp(page, run, config)")
    ensure_end = source.index("// Stage 4:", ensure_start)
    ensure = source[ensure_start:ensure_end]
    error_check = ensure.index("if (appNameError)")
    reopen = ensure.index("await recoverApp(page, config)", error_check)
    assert error_check < reopen
    assert 'state: "rejected"' in ensure[error_check:reopen]
    assert "`${appNameError}`" in ensure[error_check:reopen]


def test_utm12_does_not_auto_suffix_an_already_used_app_name() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    assert "config.appName + '.'" not in ensure
    assert 'config.appName + "-"' not in ensure


def test_utm12_reopens_apps_and_confirms_the_exact_app_after_create() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    click = ensure.index("await create.click")
    validation = ensure.index("if (appNameError)", click)
    reopen = ensure.index("if (!await recoverApp(page, config))", validation)
    submitted = ensure.index('await updateLedger(run, ledger, "submitted")', reopen)
    assert click < validation < reopen < submitted
    assert "!await waitForVerifiedAppDetail(page, config) && !await recoverApp" not in ensure


def test_utm12_rebuilds_the_rejected_create_ledger_for_a_changed_notion_app_name() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "function appCreateInputSummary(config)" in source
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    reset = ensure.index("if (await appStoreNoApps(page))")
    create_dialog = ensure.index("await openNewAppDialog(page)", reset)
    recovery = ensure[reset:create_dialog]
    assert "previousAttemptId: existingLedger.attemptId" in recovery
    assert "inputSummaryHash: sha256(appCreateInputSummary(config))" in recovery
    assert 'kind: "app-create"' in recovery
    assert 'state: "planned"' in recovery


def test_utm12_persists_and_recovers_the_three_notion_values() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "async function recoverUtm12NotionFields()" in source
    assert "run.result = {" in source
    assert "teamId: membership.teamId" in source
    assert "renewalDate: membership.renewalDate" in source
    assert "numericAppId: completion.numericAppId" in source
    assert 'command === "notion-fields"' in source


def test_utm12_exits_after_writing_a_success_result() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function main()")
    end = source.index("if (fileURLToPath", start)
    assert "() => process.exit(0)" in source[start:end]


def test_utm12_reuses_the_inherited_created_app_page() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert 'keepPageUrl.hostname === "appstoreconnect.apple.com"' in source
    assert "inheritedAppDetailUrl = await waitForVerifiedAppDetail(keepPage, null)" in source
    assert (
        "const appsTargetUrl = inheritedAppDetailUrl || persistedAppDetailUrl || APPS_URL;"
        in source
    )
    assert "acceptedPathPrefix: inheritedAppDetailPath" in source


def test_utm12_has_no_unconditional_quarter_second_waits() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "await page.waitForTimeout(250);" not in source


def test_utm12_input_contains_only_application_values() -> None:
    payload = {
        "APP_NAME": "SampleApp",
        "BUNDLE_ID": "com.example.sample",
        "APPLE_ACCOUNT_PASSWORD": "must-not-be-forwarded",
    }
    assert json.loads(utm_apps._stage_stdin("utm-12", payload)) == {
        "APP_NAME": "SampleApp",
        "BUNDLE_ID": "com.example.sample",
    }


def test_utm12_name_collision_is_recorded_and_stops_without_automatic_rename() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]

    rejection = ensure.index('state: "rejected"')
    evidence = ensure.index('rejectionEvidence: "app-name-already-used"', rejection)
    persisted = ensure.index('await writeSecureJson(ledgerPath(run, "app-create"), ledger)', evidence)
    stopped = ensure.index("throw new Error(`${appNameError}`)", persisted)
    assert rejection < evidence < persisted < stopped
    assert "uniqueAppStoreName" not in source
    assert 'recoveryEvidence: "app-name-fallback"' not in source
    assert "APP_STORE_NAME" not in source


def test_utm12_identifier_restart_flag_is_forwarded_only_when_explicit() -> None:
    payload = {"APP_NAME": "RainyDay-Kids", "BUNDLE_ID": "com.example.rainyday"}
    ordinary = json.loads(utm_apps._stage_stdin("utm-12", payload))
    restarted = json.loads(
        utm_apps._stage_stdin(
            "utm-12", {**payload, "RESET_FROM_IDENTIFIERS": True}
        )
    )

    assert "RESET_FROM_IDENTIFIERS" not in ordinary
    assert restarted["RESET_FROM_IDENTIFIERS"] is True


def test_utm12_identifier_restart_archives_only_unsubmitted_name_change_state() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function resetFromIdentifiers(run, config)")
    end = source.index("\n}\n", start) + 2
    reset = source[start:end]

    assert 'previousInput.BUNDLE_ID !== config.bundleId' in reset
    assert 'previousInput.APP_NAME === config.appName' in reset
    assert 'appCreate?.state === "planned"' in reset
    assert 'appCreate?.state === "rejected"' in reset
    assert 'appCreate.rejectionEvidence === "app-name-already-used"' in reset
    assert 'appCreate.recoveryEvidence === "app-name-fallback"' in reset
    assert "await rename(" in reset
    assert "unlink(" not in reset
    assert "delete checkpoints.appId" in reset
    assert "delete checkpoints.appStoreApp" in reset
    assert 'run.resetFrom = "identifiers"' in reset


def test_utm10_input_contains_the_notion_user_name() -> None:
    payload = {
        "APPLE_ACCOUNT_EMAIL": "mail@example.test",
        "APPLE_ACCOUNT_PASSWORD": "secret",
        "APPLE_ACCOUNT_PHONE": "+15550100",
        "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/?code=x",
        "APPLE_ACCOUNT_USER_NAME": "Marius Engel",
    }
    assert json.loads(utm_apps._stage_stdin("utm-10", payload))["userName"] == "Marius Engel"


def test_utm10_direct_sms_inbox_does_not_require_a_query_code() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    section = source.split("async function openSmsInbox", 1)[1].split("\n}\n", 1)[0]

    assert 'throw new Error("SMS URL has no query code")' not in section
    assert "if (!accessCode) return;" in section


def test_utm10_sms_query_form_does_not_depend_on_button_copy() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    section = source.split("async function openSmsInbox", 1)[1].split(
        "\n}\n", 1
    )[0]

    assert "查看短信|View Messages" not in section
    assert 'locator("xpath=ancestor::form[1]")' in section
    assert 'button[type="submit"]' in section
    assert 'input[type="submit"]' in section
    assert 'button:not([type])' in section
    assert 'await inputs[0].press("Enter")' in section


def _run_utm10_account_identity_fixture(
    bodies: list[str],
    *,
    email: str,
    user_name: str,
    wait: bool = False,
) -> subprocess.CompletedProcess[str]:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")

    def function_source(signature: str) -> str:
        start = source.index(signature)
        return source[start : source.index("\n}\n", start) + 2]

    fixture = json.dumps(
        {"bodies": bodies, "email": email, "userName": user_name, "wait": wait}
    )
    script = "\n".join(
        [
            "const TIMEOUT = 20;",
            f"const fixture = {fixture};",
            "let index = 0;",
            "let polls = 0;",
            "const frame = { locator: () => ({ innerText: async () => fixture.bodies[index] }) };",
            "const page = {",
            "  frames: () => [frame],",
            "  waitForTimeout: async () => {",
            "    polls += 1;",
            "    index = Math.min(index + 1, fixture.bodies.length - 1);",
            "  },",
            "};",
            function_source("async function accountEmailMatches(page, expectedEmail)"),
            function_source("async function accountUserNameMatches(page, expectedUserName)"),
            function_source("async function accountIdentityMatches(page, credentials)"),
            function_source("async function waitForAccountIdentity(page, credentials)"),
            "const credentials = { email: fixture.email, userName: fixture.userName };",
            "const result = fixture.wait",
            "  ? await waitForAccountIdentity(page, credentials)",
            "  : await accountIdentityMatches(page, credentials);",
            "console.log(JSON.stringify({ result, index, polls }));",
        ]
    )
    return subprocess.run(
        ["/usr/bin/env", "node", "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=ROOT,
    )


def test_utm10_account_identity_email_is_case_insensitive() -> None:
    completed = _run_utm10_account_identity_fixture(
        ["Membership details\nAPPLE.USER@EXAMPLE.TEST"],
        email="Apple.User@example.test",
        user_name="Different User",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is True


def test_utm10_account_identity_accepts_exact_case_insensitive_user_name_line() -> None:
    completed = _run_utm10_account_identity_fixture(
        ["Account\nMARIUS ENGEL\nMembership details"],
        email="missing@example.test",
        user_name="Marius Engel",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is True


def test_utm10_account_identity_rejects_user_name_substrings() -> None:
    completed = _run_utm10_account_identity_fixture(
        ["Account\nMarius Engelmann\nMembership details"],
        email="missing@example.test",
        user_name="Marius Engel",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["result"] is False


def test_utm10_waits_for_delayed_account_identity_rendering() -> None:
    completed = _run_utm10_account_identity_fixture(
        [
            "Account\nMembership details",
            "Account\nMembership details",
            "Account\nMarius Engel\nMembership details",
        ],
        email="missing@example.test",
        user_name="Marius Engel",
        wait=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"result": True, "index": 2, "polls": 2}


def test_utm10_uses_waited_email_or_user_name_account_identity_verification() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    assert 'const fields = ["email", "password", "phone", "smsUrl", "userName"];' in source
    assert "if (await accountIdentityMatches(page, credentials))" in source
    assert "if (!await waitForAccountIdentity(page, credentials))" in source
    assert "Displayed Apple Account email or user name does not match Notion" in source
    assert "return emitVerifiedLogin(page, sessionId);" in source


def test_utm10_phone_choice_uses_phone_tail_without_english_copy() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    section = source.split("async function choosePhoneText", 1)[1].split(
        "\n}\n", 1
    )[0]

    assert 'frame.getByRole("button")' in section
    assert "/text message/i" not in section
    assert 'label.replace(/\\D/g, "").endsWith(tail)' in section


def test_utm10_stale_phone_controls_are_skipped_with_a_bounded_label_read() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    helper_start = source.index("async function controlLabel(control)")
    helper_end = source.index("\n}\n", helper_start) + 2
    helper = source[helper_start:helper_end]
    phone_controls = source[
        source.index("async function phoneChoiceControls") : source.index(
            "async function loginAction"
        )
    ]
    choose_phone = source[
        source.index("async function choosePhoneText") : source.index(
            "async function enterOtp"
        )
    ]

    assert 'innerText({ timeout: STATE_TIMEOUT })' in helper
    assert 'getAttribute("aria-label", { timeout: STATE_TIMEOUT })' in helper
    assert "catch" in helper
    assert "const label = await controlLabel(choice);" in phone_controls
    assert "if (label === null) continue;" in phone_controls
    assert "const label = await controlLabel(choice);" in choose_phone
    assert "if (label === null) continue;" in choose_phone


def test_utm10_phone_page_detection_uses_control_structure_not_english_copy() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    section = source.split("async function pageState", 1)[1].split("\n}\n", 1)[0]

    assert "phoneChoiceControls(page)" in section
    assert "/text message/i" not in section


def test_utm10_otp_entry_accepts_one_whole_input_or_six_digit_inputs() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    section = source.split("async function enterOtp", 1)[1].split("\n}\n", 1)[0]

    assert "const inputs = await visible" in section
    assert "input[inputmode='numeric']" in section
    assert "input[autocomplete='one-time-code']" in section
    assert "if (inputs.length === 1)" in section
    assert "await inputs[0].fill(code);" in section
    assert "else if (inputs.length === 6)" in section
    assert "verification code inputs must be one or six" in section


def test_utm10_restores_the_original_browser_start_operation() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")

    assert "const { approvedPage, connectLocalBrowser, restartLocalBrowser }" in source
    main_source = source[source.index("async function main()") :]
    assert main_source.index("await restartLocalBrowser();") < main_source.index(
        "await currentApplePage()"
    )


def test_utm10_treats_the_last_code_rate_limit_page_as_otp_before_phone() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    page_state = source.split("async function pageState", 1)[1].split(
        "\n}\n", 1
    )[0]
    page_selection = source.split("async function currentApplePage", 1)[1].split(
        "\n}\n", 1
    )[0]

    otp_copy = "verification code digit|too many verification codes|enter the last code"
    assert otp_copy in page_state
    assert page_state.index(otp_copy) < page_state.index("phoneChoiceControls(page)")
    assert "/text message/i" not in page_state
    assert "otp: 2, phone: 3" in page_selection


def test_utm10_otp_page_uses_the_current_code_without_resend() -> None:
    source = (ROOT / "scripts" / "utm_10_login.mjs").read_text(encoding="utf-8")
    main_start = source.index("async function main()")
    main_source = source[main_start:]
    assert "requestAnotherText" not in source
    assert 'if (state === "phone") {' in main_source
    assert "state = await choosePhoneText(page, credentials.phone);" in main_source
    phone_branch = main_source.split('if (state === "phone") {', 1)[1].split(
        '} else if (state === "otp") {', 1
    )[0]
    assert phone_branch.index("choosePhoneText") < phone_branch.index("openSms")
    assert "await page.waitForTimeout(10_000);" in phone_branch
    assert phone_branch.index("choosePhoneText") < phone_branch.index(
        "waitForTimeout(10_000)"
    ) < phone_branch.index("openSms")
    direct_otp_branch = main_source.split(
        '} else if (state === "otp") {', 1
    )[1].split("\n    }", 1)[0]
    assert "await page.waitForTimeout(10_000);" in direct_otp_branch
    assert direct_otp_branch.index("waitForTimeout(10_000)") < direct_otp_branch.index(
        "openSms"
    )
    assert (
        "latest = await waitNewestSms(smsPage, credentials.smsUrl);"
        in main_source
    )
    otp_branch = main_source.rsplit('if (state === "otp") {', 1)[1].split(
        "\n    }", 1
    )[0]
    assert otp_branch.index("waitNewestSms") < otp_branch.index(
        "enterOtp"
    ) < otp_branch.index("smsPage.close")
    assert "baseline" not in main_source
    assert "datePattern" not in source
    wait_source = source.split("async function waitNewestSms", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "MutationObserver" not in wait_source
    assert wait_source.count("await smsPage.reload") == 1
    assert wait_source.count("await readMessages(smsPage)") == 2
    first_read = wait_source.index("await readMessages(smsPage)")
    refresh = wait_source.index("await smsPage.reload")
    second_read = wait_source.rindex("await readMessages(smsPage)")
    assert first_read < refresh < second_read
    phone_selector = source.split("async function choosePhoneText", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "const choices = await visible" in phone_selector
    page_selection = source.split("async function currentApplePage", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "otp: 2, phone: 3" in page_selection
    message_reader = source.split("async function readMessages", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'smsPage.locator("body").textContent' in message_reader
    assert "extractLatestSixDigitCode(text)" in message_reader
    assert "/Apple/i.test" not in message_reader
    assert '验证码：045474。' in source


def test_default_guest_preflight_syncs_current_scripts_before_verification() -> None:
    with patch.object(
        utm_apps, "sync_utm_apps_playwright_files", return_value={}
    ) as sync_scripts, patch.object(
        utm_apps,
        "_ssh_run",
        return_value=SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    ):
        utm_apps.verify_guest_scripts("192.0.2.10", "abcd")

    sync_scripts.assert_called_once_with(
        utm_apps.SHARED_DIR,
        vm_user="abcd",
        vm_ip="192.0.2.10",
    )


def test_utm12_waits_conditionally_for_bundle_id_control_to_enable() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "while (!await control.isEnabled() && Date.now() < enabledDeadline)" in source
    assert "Bundle ID control stayed disabled" in source


def test_utm12_recovers_existing_identifier_before_opening_registration_form() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    assert "async function ensureIdentifier(page, run, config)" in source
    ensure_start = source.index("async function ensureIdentifier(page, run, config)")
    ensure_end = source.index("// Stage 3:", ensure_start)
    ensure = source[ensure_start:ensure_end]
    assert "if (await recoverIdentifier(page, config))" in ensure
    assert 'await recordExistingLedger(run, "app-id-register", page' in ensure
    run_start = source.index("export async function runUtm12()")
    run_source = source[run_start:]
    assert "const identifierResult = await ensureIdentifier(workflowPage, run, config);" in run_source
    assert "appIdAttemptId = identifierResult.attemptId;" in run_source


def test_utm12_records_existing_app_without_clicking_create() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    ensure_start = source.index("async function ensureApp(page, run, config)")
    ensure_end = source.index("// Stage 4:", ensure_start)
    ensure = source[ensure_start:ensure_end]
    assert 'await recordExistingLedger(run, "app-create", page' in ensure
    assert "allowUniqueNameRecovery: true" in ensure


def test_utm12_checks_the_visible_existing_app_before_waiting_for_search() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function appRows(page, config")
    end = source.index("async function openExistingApp", start)
    app_rows = source[start:end]
    recovery = app_rows.index("let recoveryApps = await exactExistingAppLinks")
    wait = app_rows.index("await Promise.race")
    assert recovery < wait


def test_utm12_recovers_existing_app_from_unique_distribution_link_before_search() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    helper_start = source.index("async function exactExistingAppLinks(page, config)")
    start = source.index("async function appRows(page, config", helper_start)
    end = source.index("async function openExistingApp", start)
    helper = source[helper_start:start]
    app_rows = source[start:end]
    exact_link = helper.index('page.locator("a[href]")')
    href_check = helper.index("/^\\/apps\\/\\d+\\/distribution$/")
    recovery_gate = app_rows.index("if (recoveryApps.length === 1)")
    search_wait = app_rows.index("await Promise.race")
    assert exact_link < href_check
    assert recovery_gate < search_wait


def test_utm12_uses_existing_app_link_recovery_on_every_apps_page_check() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function appRows(page, config")
    end = source.index("async function openExistingApp", start)
    app_rows = source[start:end]
    assert "if (recoveryApps.length === 1)" in app_rows
    assert "if (allowUniqueNameRecovery && recoveryApps.length === 1)" not in app_rows


def test_utm12_existing_app_link_match_accepts_href_query_and_fragment() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function exactExistingAppLinks(page, config)")
    end = source.index("async function appRows(page, config", start)
    app_rows = source[start:end]
    assert (
        'const distributionPath = url.pathname.replace(/\\/+$/, "");' in app_rows
    )
    assert '/^\\/apps\\/\\d+\\/distribution$/.test(distributionPath)' in app_rows


def test_utm12_existing_app_recovery_uses_unique_exact_text_link_and_valid_href() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function exactExistingAppLinks(page, config)")
    end = source.index("async function appRows(page, config", start)
    app_rows = source[start:end]
    assert "const recoveryNames = await visibleInFrames" in app_rows
    assert 'frame.getByText(config.appName, { exact: true })' in app_rows
    assert 'const recoveryLink = recoveryName.locator("xpath=ancestor-or-self::a[1]");' in app_rows
    assert "if (await recoveryLink.count() !== 1) continue;" in app_rows


def test_utm12_existing_app_recovery_matches_exact_link_href_without_role_or_text_dependency() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function exactExistingAppLinks(page, config)")
    end = source.index("async function appRows(page, config", start)
    app_rows = source[start:end]
    assert 'frame.locator("a[href]")' in app_rows
    assert 'const text = (await link.innerText().catch(() => "")).trim();' in app_rows
    assert 'if (text !== config.appName) continue;' in app_rows


def test_utm12_app_rows_read_visible_distribution_href_directly_from_dom() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function exactExistingAppLinks(page, config)")
    end = source.index("async function appRows(page, config", start)
    app_rows = source[start:end]
    assert "const recoveryCandidates = await page.locator(\"a[href]\").evaluateAll(" in app_rows
    assert "text: String(link.textContent || \"\").trim()," in app_rows
    assert "href: String(link.href || link.getAttribute(\"href\") || \"\")," in app_rows
    assert "candidate.text !== config.appName" in app_rows


def test_utm12_rechecks_existing_app_after_spa_content_becomes_ready() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function appRows(page, config")
    end = source.index("async function openExistingApp", start)
    app_rows = source[start:end]
    wait = app_rows.index("await Promise.race")
    second_recovery = app_rows.index(
        "recoveryApps = await exactExistingAppLinks(page, config);", wait
    )
    filter_call = app_rows.index(
        'await filterListByExactValue(page, config.appName, "App Store app")'
    )
    assert wait < second_recovery < filter_call


def test_utm12_accepts_existing_version_detail_marker_from_live_page() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function verifyAppDetail(page, _config)")
    end = source.index("async function waitForVerifiedAppDetail", start)
    verify = source[start:end]
    assert "/iOS(?: App)?(?: Version)?\\s+1\\.0\\b/" in verify


def test_utm12_skips_app_search_when_already_on_verified_existing_detail() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function ensureApp(page, run, config)")
    end = source.index("// Stage 4:", start)
    ensure = source[start:end]
    detail = ensure.index("if (await waitForVerifiedAppDetail(page, config))")
    search = ensure.index("if (await openExistingApp(page, config")
    assert detail < search


def test_utm12_replays_verified_app_detail_from_persisted_live_session_evidence() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("export async function runUtm12()")
    run_source = source[start:]
    persisted = run_source.index("const persistedResult = resultFromPersistedRun(run);")
    target = run_source.index(
        "const appsTargetUrl = inheritedAppDetailUrl || persistedAppDetailUrl || APPS_URL;"
    )
    ensure = run_source.index("const appResult = await ensureApp(apps, run, config);")
    assert persisted < target < ensure


def test_utm12_persists_validated_application_input_for_utm13_handoff() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("async function loadAppConfig()")
    end = source.index("// Single-tab Playwright", start)
    loader = source[start:end]
    assert "await writeSecureJson(path.join(RUNTIME_ROOT, \"input.json\")," in loader
    assert "APP_NAME: config.appName" in loader
    assert "BUNDLE_ID: config.bundleId" in loader


def test_utm12_verified_replay_checks_live_app_detail_and_returns_before_full_flow() -> None:
    source = (ROOT / "scripts" / "utm_12_one.mjs").read_text(encoding="utf-8")
    start = source.index("export async function runUtm12()")
    run_source = source[start:]
    replay = run_source.index("const replay = await replayVerifiedUtm12(context, run);")
    account = run_source.index("verifyDeveloperAccountStage(context, run)")
    assert replay < account
    assert "if (replay) return replay;" in run_source[replay:account]
    assert "await waitForVerifiedAppDetail(page, null)" in source


def test_utm12_guest_stage_allows_transport_grace_after_180_second_guest_timeout() -> None:
    observed = {}

    def runner(*_args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise RuntimeError("stop after capturing the timeout")

    with pytest.raises(RuntimeError, match="stop after capturing"):
        utm_apps.run_guest_stage(
            stage="utm-12",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"APP_NAME": "SampleApp", "BUNDLE_ID": "com.example.sample"},
            runner=runner,
            password_env={},
        )

    assert observed["timeout"] == 185


def test_utm13_guest_stage_keeps_the_60_second_guest_limit_with_transport_grace() -> None:
    observed = {}

    def runner(*_args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise RuntimeError("stop after capturing the timeout")

    with pytest.raises(RuntimeError, match="stop after capturing"):
        utm_apps.run_guest_stage(
            stage="utm-13",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"RUN_ID": "run-1"},
            runner=runner,
            password_env={},
        )

    assert observed["timeout"] == 65


def test_guest_timeout_is_reported_instead_of_unclassified() -> None:
    completed = SimpleNamespace(
        returncode=142,
        stdout=b"",
        stderr=b"UTM_10_ERROR=TIMEOUT_60_SECONDS\n",
    )

    with pytest.raises(
        utm_apps.AppleWebError,
        match="UTM_APPS_STAGE_FAILED=UTM_10:TIMEOUT_60_SECONDS",
    ):
        utm_apps.run_guest_stage(
            stage="utm-10",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={
                "APPLE_ACCOUNT_EMAIL": "mail@example.test",
                "APPLE_ACCOUNT_PASSWORD": "secret",
                "APPLE_ACCOUNT_PHONE": "+15550100",
                "APPLE_ACCOUNT_SMS_URL": "https://sms.example.test/?code=x",
                "APPLE_ACCOUNT_USER_NAME": "Marius Engel",
            },
            runner=lambda *_args, **_kwargs: completed,
            password_env={},
        )


def test_guest_stage_preserves_the_complete_actual_error_text() -> None:
    detail = (
        'Create button did not become enabled — 当前页面仍显示 “No Apps”; '
        + "x" * 220
    )
    completed = SimpleNamespace(
        returncode=1,
        stdout=b"",
        stderr=f"UTM_12_ERROR={detail}\n".encode(),
    )

    with pytest.raises(utm_apps.AppleWebError) as captured:
        utm_apps.run_guest_stage(
            stage="utm-12",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"APP_NAME": "SampleApp", "BUNDLE_ID": "com.example.sample"},
            runner=lambda *_args, **_kwargs: completed,
            password_env={},
        )

    assert str(captured.value) == f"UTM_APPS_STAGE_FAILED=UTM_12:{detail}"


def test_guest_stage_preserves_playwright_call_log_after_error_marker() -> None:
    stderr = (
        "UTM_12_ERROR=locator.check: Timeout 5000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator(\"input[type=checkbox]\")\n"
        "  - element is not enabled\n"
    )
    completed = SimpleNamespace(returncode=1, stdout=b"", stderr=stderr.encode())

    with pytest.raises(utm_apps.AppleWebError) as captured:
        utm_apps.run_guest_stage(
            stage="utm-12",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"APP_NAME": "SampleApp", "BUNDLE_ID": "com.example.sample"},
            runner=lambda *_args, **_kwargs: completed,
            password_env={},
        )

    assert str(captured.value) == (
        "UTM_APPS_STAGE_FAILED=UTM_12:locator.check: Timeout 5000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator(\"input[type=checkbox]\")\n"
        "  - element is not enabled"
    )


def test_notion_fields_stage_preserves_the_complete_actual_error_text() -> None:
    detail = "Notion readback mismatch：生日 2000/2/29 != 页面 2000/02/28；" + "y" * 220
    completed = SimpleNamespace(
        returncode=1,
        stdout=b"",
        stderr=f"UTM_12_ERROR={detail}\n".encode(),
    )

    with pytest.raises(utm_apps.AppleWebError) as captured:
        utm_apps.run_utm12_notion_fields(
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            runner=lambda *_args, **_kwargs: completed,
            password_env={},
        )

    assert str(captured.value) == (
        f"UTM_APPS_STAGE_FAILED=UTM_12_NOTION_FIELDS:{detail}"
    )


def test_utm_apps_entry_preserves_complete_error_detail() -> None:
    source = (ROOT / "scripts" / "utm_apps.py").read_text(encoding="utf-8")
    main_source = source[source.index("def main()") :]
    assert "preserve_error_detail=True" in main_source


def test_utm12_guest_timeout_reports_the_stage_specific_180_second_marker() -> None:
    completed = SimpleNamespace(
        returncode=142,
        stdout=b"",
        stderr=b"UTM_12_ERROR=TIMEOUT_180_SECONDS\n",
    )

    with pytest.raises(
        utm_apps.AppleWebError,
        match="UTM_APPS_STAGE_FAILED=UTM_12:TIMEOUT_180_SECONDS",
    ):
        utm_apps.run_guest_stage(
            stage="utm-12",
            vm_name="abcd",
            vm_ip="192.0.2.10",
            vm_user="abcd",
            payload={"APP_NAME": "SampleApp", "BUNDLE_ID": "com.example.sample"},
            runner=lambda *_args, **_kwargs: completed,
            password_env={},
        )


def test_stage_selector_can_resume_from_utm12_through_utm13() -> None:
    parser = utm_apps.build_parser()
    args = parser.parse_args(
        [
            "--page-title",
            "SowSheet-igec",
            "--vm-name",
            "igec",
            "--vm-ip",
            "192.168.64.52",
            "--vm-user",
            "igec",
            "--stage",
            "utm-12-onward",
        ]
    )
    assert args.stage == "utm-12-onward"


def test_stage_selector_can_restart_utm12_from_identifiers() -> None:
    parser = utm_apps.build_parser()
    args = parser.parse_args(
        [
            "--page-title",
            "RainyDay-Kids-rbqi",
            "--vm-name",
            "rbqi",
            "--vm-ip",
            "192.168.64.60",
            "--vm-user",
            "rbqi",
            "--stage",
            "utm-12-identifiers-onward",
        ]
    )
    assert args.stage == "utm-12-identifiers-onward"


def test_stage_selector_can_skip_login_and_resume_from_utm11() -> None:
    parser = utm_apps.build_parser()
    args = parser.parse_args(
        [
            "--page-title",
            "SowSheet-igec",
            "--vm-name",
            "igec",
            "--vm-ip",
            "192.168.64.52",
            "--vm-user",
            "igec",
            "--stage",
            "utm-11-onward",
        ]
    )
    assert args.stage == "utm-11-onward"


def test_utm11_onward_never_invokes_the_login_stage() -> None:
    source = (ROOT / "scripts" / "utm_apps.py").read_text(encoding="utf-8")
    start = source.index('if requested_stage not in {"all", "utm-11-onward"}:')
    end = source.index('print("UTM Apps 已完成"', start)
    branch = source[start:end]
    login_guard = branch.index('if requested_stage == "all":')
    login_call = branch.index('stage="utm-10"', login_guard)
    business_call = branch.index('stage="utm-11"', login_call)

    assert login_guard < login_call < business_call
    assert branch.index("edge_session = _session_from_result(stage_11)") > business_call


def test_stage_selector_can_run_only_utm13_for_validation() -> None:
    parser = utm_apps.build_parser()
    args = parser.parse_args(
        [
            "--page-title",
            "SowSheet-igec",
            "--vm-name",
            "igec",
            "--vm-ip",
            "192.168.64.52",
            "--vm-user",
            "igec",
            "--stage",
            "utm-13",
        ]
    )
    assert args.stage == "utm-13"


def test_utm12_onward_runs_utm12_but_skips_only_equal_notion_writes() -> None:
    source = (ROOT / "scripts" / "utm_apps.py").read_text(encoding="utf-8")
    start = source.index('if requested_stage in {\n        "utm-12",')
    end = source.index('if requested_stage not in {"all", "utm-11-onward"}:', start)
    branch = source[start:end]
    notion_check = branch.index("registered_utm12 = _registered_utm12_fields(notion, page_title)")
    stage_call = branch.index('stage="utm-12"')
    assert notion_check < stage_call
    assert "stage_12 = run_guest_stage(" in branch
    assert "if registered_utm12:" in branch
    assert "_write_membership(" in branch
    assert "_write_app_id(" in branch
    assert '_print_stage_result(' in branch
    assert 'stage="utm-13"' in branch


def test_delivery_operations_have_a_30_second_timeout() -> None:
    source = (ROOT / "scripts" / "utm_apps_delivery.py").read_text(encoding="utf-8")
    assert "timeout=60" not in source


def test_utm12_result_reads_membership_and_numeric_app_id() -> None:
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "UTM_12": "verified",
                    "team_id": "ABCDE12345",
                    "renewal_date": "August 11, 2027",
                    "NUMERIC_APP_ID": "1234567890",
                }
            ),
            json.dumps(
                {
                    "edge_pid": 42,
                    "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
                }
            ),
        )
    )
    result = utm_apps._parse_playwright_guest_result("utm-12", stdout, "abcd")
    assert result.markers == ("MEMBERSHIP_DETAILS=verified", "UTM_12=verified")
    assert result.data["team_id"] == "ABCDE12345"
    assert result.data["renewal_date"] == "August 11, 2027"
    assert result.data["numeric_app_id"] == "1234567890"


class FakeNotion:
    def __init__(self) -> None:
        self.fields = {
            ("账号信息", "修改后的密码："): "secret",
            ("账号信息", "初始密码："): "",
            ("账号信息", "用户名："): "Marius Engel",
            ("账号信息", "邮箱："): "mail@example.test",
            ("账号信息", "电话："): "+15550100",
            ("账号信息", "电话短信接收平台："): "https://sms.example.test/",
            ("账号信息", "team ID:"): "",
            ("账号信息", "Renewal date："): "",
            ("账号信息", "APP_ID："): "",
            ("应用信息", "应用名: "): "SampleApp",
            ("应用信息", "正式包名: "): "com.example.sample",
        }
        self.read_section_calls = 0
        self.write_section_calls = 0

    def verify_parent(self, _title: str) -> None:
        return None

    def read_field(self, _page: str, section: str, label: str) -> str:
        return self.fields[(section, label)]

    def set_field(
        self,
        _page: str,
        section: str,
        label: str,
        value: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        current = self.fields[(section, label)]
        if current and current != value and not replace_existing:
            raise RuntimeError("conflict")
        self.fields[(section, label)] = value
        return current != value

    def read_section(self, _page: str, section: str) -> str:
        self.read_section_calls += 1
        labels = [label for current_section, label in self.fields if current_section == section]
        return "\n".join(label + self.fields[(section, label)] for label in labels)

    def write_section(
        self,
        _page: str,
        section: str,
        text: str,
        *,
        replace_existing: bool = False,
    ) -> bool:
        assert replace_existing
        self.write_section_calls += 1
        before = self.read_section(_page, section)
        for line in text.splitlines():
            labels = [label for current_section, label in self.fields if current_section == section and line.startswith(label)]
            assert len(labels) == 1
            label = labels[0]
            self.fields[(section, label)] = line[len(label):]
        return before != text


def test_explicit_utm12_stage_runs_no_other_stage(capsys) -> None:
    notion = FakeNotion()
    args = SimpleNamespace(
        run_id=None,
        page_title="SampleApp-abcd",
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
        stage="utm-12",
    )
    stage_calls = []

    def fake_stage(**kwargs):
        stage_calls.append(kwargs["stage"])
        return utm_apps.GuestWorkflowResult(
            ("MEMBERSHIP_DETAILS=verified", "UTM_12=verified"),
            {
                "team_id": "ABCDE12345",
                "renewal_date": "August 11, 2027",
                "numeric_app_id": "1234567890",
                "edge_pid": 42,
                "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
            },
        )

    with patch.object(
        utm_apps,
        "_resolve_context",
        return_value=(True, "direct-v1-abcd-123456789012345678901234", "host", "SampleApp-abcd", "SampleApp"),
    ), patch.object(utm_apps, "verify_guest_scripts"), patch.object(
        utm_apps, "run_guest_stage", side_effect=fake_stage
    ):
        assert utm_apps.run(args, api=notion) == 0

    assert stage_calls == ["utm-12"]
    assert notion.fields[("账号信息", "team ID:")] == "ABCDE12345"
    assert notion.fields[("账号信息", "Renewal date：")] == "August 11, 2027"
    assert notion.fields[("账号信息", "APP_ID：")] == "1234567890"
    output = capsys.readouterr().out
    assert "UTM-12 App Store Connect步骤和app信息登记步骤已完成" in output
    assert "UTM_10=verified" not in output
    assert "UTM_11=verified" not in output
    assert "UTM_13=verified" not in output


def test_explicit_notion_fields_stage_only_writes_and_reads_back(capsys) -> None:
    notion = FakeNotion()
    args = SimpleNamespace(
        run_id=None,
        page_title="SampleApp-abcd",
        vm_name="abcd",
        vm_ip="192.0.2.10",
        vm_user="abcd",
        stage="notion-fields",
    )
    result = utm_apps.GuestWorkflowResult(
        ("MEMBERSHIP_DETAILS=verified", "UTM_12=verified"),
        {
            "team_id": "ABCDE12345",
            "renewal_date": "August 11, 2027",
            "numeric_app_id": "1234567890",
            "edge_pid": 42,
            "edge_websocket": "ws://127.0.0.1:9222/devtools/browser/session",
        },
    )

    with patch.object(
        utm_apps,
        "_resolve_context",
        return_value=(True, "direct-v1-abcd-123456789012345678901234", "host", "SampleApp-abcd", "SampleApp"),
    ), patch.object(
        utm_apps, "verify_utm12_notion_fields_scripts"
    ) as verify, patch.object(
        utm_apps, "run_utm12_notion_fields", return_value=result
    ) as recover, patch.object(
        utm_apps, "verify_guest_scripts"
    ) as full_preflight, patch.object(
        utm_apps, "run_guest_stage"
    ) as full_stage:
        assert utm_apps.run(args, api=notion) == 0

    verify.assert_called_once()
    recover.assert_called_once()
    full_preflight.assert_not_called()
    full_stage.assert_not_called()
    assert notion.fields[("账号信息", "team ID:")] == "ABCDE12345"
    assert notion.fields[("账号信息", "Renewal date：")] == "August 11, 2027"
    assert notion.fields[("账号信息", "APP_ID：")] == "1234567890"
    assert notion.write_section_calls == 1
    assert notion.read_section_calls == 3
    output = capsys.readouterr().out
    assert "UTM-12 App Store Connect步骤和app信息登记步骤已完成" in output
    assert "UTM_10=verified" not in output
    assert "UTM_11=verified" not in output
    assert "UTM_13=verified" not in output
