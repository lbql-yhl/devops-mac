#!/usr/bin/env python3
"""Guest entry for scripted UTM-10 through UTM-13 Apple web workflows.

All workflow values arrive through stdin JSON.  The helper emits exactly one
JSON result line and never echoes account values, application values, phone
numbers, SMS URLs, verification codes, or the configured guest password.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlparse, urlsplit

try:
    from edge_accessibility import (
        EdgeAXBackend,
        edge_session_identity,
        reuse_utm10_edge,
        start_utm10_edge,
    )
except ModuleNotFoundError:
    from scripts.edge_accessibility import (
        EdgeAXBackend,
        edge_session_identity,
        reuse_utm10_edge,
        start_utm10_edge,
    )


ACCOUNT_URL = "https://developer.apple.com/account/"
BUSINESS_URL = "https://appstoreconnect.apple.com/business"
SMALL_BUSINESS_URL = "https://developer.apple.com/app-store/small-business-program/"
IDENTIFIERS_URL = "https://developer.apple.com/account/resources/identifiers/list"
IDENTIFIER_ADD_URL = "https://developer.apple.com/account/resources/identifiers/add/bundleId"
APPS_URL = "https://appstoreconnect.apple.com/apps"
CERTIFICATES_URL = "https://developer.apple.com/account/resources/certificates/list"
CERTIFICATE_ADD_URL = "https://developer.apple.com/account/resources/certificates/add"
PROFILES_URL = "https://developer.apple.com/account/resources/profiles/list"
PROFILE_ADD_URL = "https://developer.apple.com/account/resources/profiles/add"

SUCCESS_MESSAGE_1 = "Thank you for your submission."
SUCCESS_MESSAGE_2 = (
    "We've received your App Store Small Business Program enrollment and will "
    "email you about your status soon."
)

QUESTIONS = (
    (
        "Have you reviewed and accepted the latest Paid Applications Agreement",
        "Yes, I have accepted.",
    ),
    (
        "Do you have majority (over 50%) corporate, individual, or partnership interest",
        "No",
    ),
    (
        "Does another Apple Developer Program member have majority (over 50%)",
        "No",
    ),
    (
        "Do you have ultimate decision-making authority over another Apple Developer Program member account",
        "No",
    ),
    (
        "Does another Apple Developer Program member have ultimate decision-making authority over your account",
        "No",
    ),
)

HTML_EMAIL_PROBE_SCRIPT = r"""
// PROBE_KIND_EMAIL
const visible = (element) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return !element.disabled && element.type !== "hidden" &&
    style.display !== "none" && style.visibility !== "hidden" &&
    rect.width > 0 && rect.height > 0;
};
const unique = (items) => Array.from(new Set(items));
const preferred = unique(Array.from(document.querySelectorAll(
  'input#account_name_text_field,' +
  'input[name="accountName"],' +
  'input[autocomplete="username"],' +
  'input[type="email"],' +
  'input[placeholder="Email or Phone Number"]'
))).filter(visible);
const fallback = Array.from(document.querySelectorAll('input')).filter((element) =>
  visible(element) && !["password", "hidden", "checkbox", "radio", "button", "submit"].includes(element.type)
);
const candidates = preferred.length ? preferred : fallback;
const passwordCount = Array.from(
  document.querySelectorAll('input[type="password"]')
).filter(visible).length;
return {status: "probed", candidate_count: candidates.length,
        password_count: passwordCount};
"""

HTML_EMAIL_INJECT_SCRIPT = r"""
// INJECT_KIND_EMAIL
const expected = arguments[0];
const visible = (element) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return !element.disabled && element.type !== "hidden" &&
    style.display !== "none" && style.visibility !== "hidden" &&
    rect.width > 0 && rect.height > 0;
};
const unique = (items) => Array.from(new Set(items));
const preferred = unique(Array.from(document.querySelectorAll(
  'input#account_name_text_field,' +
  'input[name="accountName"],' +
  'input[autocomplete="username"],' +
  'input[type="email"],' +
  'input[placeholder="Email or Phone Number"]'
))).filter(visible);
const fallback = Array.from(document.querySelectorAll('input')).filter((element) =>
  visible(element) && !["password", "hidden", "checkbox", "radio", "button", "submit"].includes(element.type)
);
const candidates = preferred.length ? preferred : fallback;
const passwordCount = Array.from(
  document.querySelectorAll('input[type="password"]')
).filter(visible).length;
if (candidates.length !== 1) {
  return {status: "blocked", candidate_count: candidates.length,
          password_count: passwordCount};
}
const field = candidates[0];
const setter = Object.getOwnPropertyDescriptor(
  HTMLInputElement.prototype, "value"
).set;
field.focus();
setter.call(field, expected);
field.dispatchEvent(new Event("input", {bubbles: true}));
field.dispatchEvent(new Event("change", {bubbles: true}));
return {status: "injected", candidate_count: 1, password_count: 0};
"""

HTML_EMAIL_VERIFY_SCRIPT = r"""
// VERIFY_KIND_EMAIL
const expected = arguments[0];
const visible = (element) => {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return !element.disabled && element.type !== "hidden" &&
    style.display !== "none" && style.visibility !== "hidden" &&
    rect.width > 0 && rect.height > 0;
};
const preferred = Array.from(new Set(Array.from(document.querySelectorAll(
  'input#account_name_text_field,' +
  'input[name="accountName"],' +
  'input[autocomplete="username"],' +
  'input[type="email"],' +
  'input[placeholder="Email or Phone Number"]'
)))).filter(visible);
const fallback = Array.from(document.querySelectorAll('input')).filter((element) =>
  visible(element) && !["password", "hidden", "checkbox", "radio", "button", "submit"].includes(element.type)
);
const candidates = preferred.length ? preferred : fallback;
const passwordCount = Array.from(
  document.querySelectorAll('input[type="password"]')
).filter(visible).length;
return {status: "verified", candidate_count: candidates.length,
        password_count: passwordCount,
        matches: candidates.length === 1 && candidates[0].value === expected};
"""

HTML_EMAIL_CONTINUE_CLICK_SCRIPT = r"""
// CLICK_KIND_EMAIL_CONTINUE
const interactive = (element) => {
  if (element.disabled || element.getAttribute("aria-disabled") === "true") return false;
  let current = element;
  while (current) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) === 0 || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const candidates = Array.from(document.querySelectorAll(
  'button,input[type="button"],input[type="submit"]'
)).filter((element) => {
  const label = (element.innerText || element.value || "").trim();
  return interactive(element) && label === "Continue";
});
if (candidates.length !== 1) {
  return {status: "blocked", candidate_count: candidates.length};
}
candidates[0].click();
return {status: "clicked", candidate_count: 1};
"""

HTML_PASSWORD_PROBE_SCRIPT = r"""
// PROBE_KIND_PASSWORD
const interactive = (element) => {
  if (element.disabled || element.getAttribute("aria-disabled") === "true") return false;
  let current = element;
  while (current) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) === 0 || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const candidates = Array.from(document.querySelectorAll('input[type="password"]'))
  .filter(interactive);
return {status: "probed", candidate_count: candidates.length};
"""

HTML_PASSWORD_INJECT_SCRIPT = r"""
// INJECT_KIND_PASSWORD
const expected = arguments[0];
const interactive = (element) => {
  if (element.disabled || element.getAttribute("aria-disabled") === "true") return false;
  let current = element;
  while (current) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) === 0 || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const candidates = Array.from(document.querySelectorAll('input[type="password"]'))
  .filter(interactive);
if (candidates.length !== 1) {
  return {status: "blocked", candidate_count: candidates.length};
}
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
candidates[0].focus();
setter.call(candidates[0], expected);
candidates[0].dispatchEvent(new Event("input", {bubbles: true}));
candidates[0].dispatchEvent(new Event("change", {bubbles: true}));
return {status: "injected", candidate_count: 1};
"""

HTML_PASSWORD_VERIFY_SCRIPT = r"""
// VERIFY_KIND_PASSWORD
const expected = arguments[0];
const interactive = (element) => {
  if (element.disabled || element.getAttribute("aria-disabled") === "true") return false;
  let current = element;
  while (current) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) === 0 || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const candidates = Array.from(document.querySelectorAll('input[type="password"]'))
  .filter(interactive);
return {status: "verified", candidate_count: candidates.length,
        matches: candidates.length === 1 && candidates[0].value === expected};
"""

HTML_REMEMBER_ME_CHECK_SCRIPT = r"""
// CHECK_KIND_REMEMBER_ME
const interactive = (element) => {
  if (element.disabled || element.getAttribute("aria-disabled") === "true") return false;
  let current = element;
  while (current) {
    const style = window.getComputedStyle(current);
    if (style.display === "none" || style.visibility === "hidden" ||
        Number(style.opacity) === 0 || current.getAttribute("aria-hidden") === "true") {
      return false;
    }
    current = current.parentElement;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const labels = Array.from(document.querySelectorAll("label")).filter((label) =>
  (label.textContent || "").trim() === "Remember me" && interactive(label)
);
if (labels.length !== 1) {
  return {status: "blocked", candidate_count: labels.length, checked: false};
}
const label = labels[0];
let checkbox = label.control || label.querySelector('input[type="checkbox"]');
if (!checkbox && label.htmlFor) checkbox = document.getElementById(label.htmlFor);
if (!checkbox && label.parentElement) {
  checkbox = label.parentElement.querySelector(
    'input[type="checkbox"],[role="checkbox"]'
  );
}
if (!checkbox) {
  return {status: "blocked", candidate_count: 0, checked: false};
}
const readChecked = () => checkbox.matches('input[type="checkbox"]')
  ? checkbox.checked === true
  : checkbox.getAttribute("aria-checked") === "true";
if (!readChecked()) label.click();
const checked = readChecked();
return {status: checked ? "checked" : "blocked",
        candidate_count: 1, checked};
"""

HTML_REMEMBER_ME_VERIFY_SCRIPT = HTML_REMEMBER_ME_CHECK_SCRIPT.replace(
    "// CHECK_KIND_REMEMBER_ME", "// VERIFY_KIND_REMEMBER_ME"
).replace(
    "if (!readChecked()) label.click();",
    "",
).replace(
    'status: checked ? "checked" : "blocked"',
    'status: checked ? "verified" : "blocked"',
)


class AppleWebWorkflowError(RuntimeError):
    """Raised when a workflow cannot prove a unique safe browser state."""


def _safe_error_detail(error: Exception) -> str | None:
    detail = str(error)
    if re.fullmatch(r"[A-Z][A-Z0-9_]*(?:=[A-Z0-9_.,:|/-]+)?", detail):
        return detail
    return None


class GuestAttemptLedger:
    """Mode-600 guest ledger that makes each irreversible browser action one-shot."""

    def __init__(
        self,
        *,
        run_id: str,
        vm_name: str,
        identity: Mapping[str, str],
        state_root: Path | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", run_id):
            raise AppleWebWorkflowError("RUN_ID_INVALID")
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise AppleWebWorkflowError("VM_NAME_INVALID")
        root = state_root or Path(f"/Users/{vm_name}/Downloads/utm-apple-web-state")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / f"{run_id}.json"
        self.identity_sha256 = hashlib.sha256(
            json.dumps(dict(identity), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise AppleWebWorkflowError("GUEST_ATTEMPT_LEDGER_UNSAFE")
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                self.state.get("run_id") != run_id
                or self.state.get("vm_name") != vm_name
                or self.state.get("identity_sha256") != self.identity_sha256
                or not isinstance(self.state.get("attempts"), dict)
            ):
                raise AppleWebWorkflowError("GUEST_ATTEMPT_LEDGER_IDENTITY_MISMATCH")
        else:
            self.state = {
                "version": 1,
                "run_id": run_id,
                "vm_name": vm_name,
                "identity_sha256": self.identity_sha256,
                "attempts": {},
            }
            self._save()

    def _save(self) -> None:
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(self.state, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)

    def _entry(self, name: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
            raise AppleWebWorkflowError("ATTEMPT_NAME_INVALID")
        attempts = self.state["attempts"]
        if name not in attempts:
            attempts[name] = {"attempt_id": uuid.uuid4().hex, "status": "planned"}
            self._save()
        entry = attempts[name]
        if entry.get("status") not in {"planned", "clicking", "unknown", "succeeded"}:
            raise AppleWebWorkflowError("ATTEMPT_STATUS_INVALID")
        return entry

    def attempt_id(self, name: str) -> str:
        return self._entry(name)["attempt_id"]

    def status(self, name: str) -> str:
        return self._entry(name)["status"]

    def begin(self, name: str) -> bool:
        entry = self._entry(name)
        if entry["status"] != "planned":
            return False
        entry["status"] = "clicking"
        self._save()
        return True

    def complete(self, name: str) -> None:
        entry = self._entry(name)
        entry["status"] = "succeeded"
        self._save()


class GuestAppsSessionLedger:
    """Bind all merged stages to one run, VM, app, and Edge CDP session."""

    STAGES = (
        "utm-10",
        "utm-11",
        "utm-12-membership",
        "utm-12-apps",
        "utm-13",
    )

    def __init__(self, payload: Mapping[str, Any]) -> None:
        run_id = _required(payload, "RUN_ID")
        vm_name = _required(payload, "VM_NAME")
        app_name = _required(payload, "APP_NAME")
        bundle_id = _required(payload, "BUNDLE_ID")
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", run_id):
            raise AppleWebWorkflowError("RUN_ID_INVALID")
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise AppleWebWorkflowError("VM_NAME_INVALID")
        identity = {
            "run_id": run_id,
            "vm_name": vm_name,
            "app_name": app_name,
            "bundle_id": bundle_id,
        }
        identity_sha256 = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        root = Path(
            f"/Users/{vm_name}/Downloads/AppleAccountScriptsBackup/.utm-apps-state"
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / f"{run_id}.json"
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise AppleWebWorkflowError("UTM_APPS_LEDGER_UNSAFE")
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                self.state.get("identity_sha256") != identity_sha256
                or not isinstance(self.state.get("completed_stages"), list)
            ):
                raise AppleWebWorkflowError("UTM_APPS_LEDGER_IDENTITY_MISMATCH")
        else:
            self.state = {
                "version": 1,
                "identity_sha256": identity_sha256,
                "edge_pid": None,
                "edge_websocket": None,
                "completed_stages": [],
            }
            self._save()

    def _save(self) -> None:
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(self.state, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)

    def session(self) -> tuple[int, str] | None:
        pid = self.state.get("edge_pid")
        websocket = self.state.get("edge_websocket")
        if pid is None and websocket is None:
            return None
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(websocket, str)
            or not websocket.startswith("ws://127.0.0.1:9222/")
        ):
            raise AppleWebWorkflowError("UTM_APPS_LEDGER_SESSION_INVALID")
        return pid, websocket

    def bind_session(self, pid: int, websocket: str) -> None:
        existing = self.session()
        if existing is not None and existing != (pid, websocket):
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        self.state["edge_pid"] = pid
        self.state["edge_websocket"] = websocket
        self._save()

    def require_stage(self, stage: str) -> None:
        if stage not in self.STAGES:
            raise AppleWebWorkflowError("UTM_APPS_STAGE_INVALID")
        completed = self.state["completed_stages"]
        index = self.STAGES.index(stage)
        required = list(self.STAGES[:index])
        if completed[:index] != required:
            raise AppleWebWorkflowError("UTM_APPS_STAGE_ORDER_INVALID")
        if len(completed) > index and completed[index] != stage:
            raise AppleWebWorkflowError("UTM_APPS_STAGE_ORDER_INVALID")

    def complete_stage(self, stage: str) -> None:
        self.require_stage(stage)
        completed = self.state["completed_stages"]
        if stage not in completed:
            completed.append(stage)
            self._save()

    @property
    def completed_stages(self) -> tuple[str, ...]:
        return tuple(self.state["completed_stages"])


def _required(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AppleWebWorkflowError(f"PAYLOAD_FIELD_MISSING={key}")
    return value


def _fetch_otp(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AppleWebWorkflowError("OTP_URL_INVALID")
    for candidate in (url,):
        request = urllib.request.Request(
            candidate,
            headers={
                "User-Agent": "submission-automation/1",
                "Accept": "application/json,text/plain,text/html",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError):
            continue
        try:
            decoded = json.loads(body)
        except (TypeError, ValueError):
            decoded = None
        messages = decoded.get("messages") if isinstance(decoded, dict) else None
        if isinstance(messages, list):
            codes: list[str] = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_text = " ".join(
                    str(message.get(key) or "")
                    for key in ("body", "message", "content", "text", "sms")
                )
                codes.extend(
                    re.findall(r"(?<!\d)(\d{6})(?!\d)", message_text)
                )
            if codes:
                return codes[-1]
        codes = re.findall(r"(?<!\d)(\d{6})(?!\d)", body)
        if codes:
            return codes[-1]
    raise AppleWebWorkflowError("OTP_MATCH_COUNT=0")


def _attach_existing_edge_with_selenium() -> Any:
    try:
        import selenium  # noqa: F401
    except ModuleNotFoundError:
        installed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "selenium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
        if installed.returncode != 0:
            raise AppleWebWorkflowError("SELENIUM_INSTALL_FAILED")
        verified = subprocess.run(
            [sys.executable, "-c", "import selenium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if verified.returncode != 0:
            raise AppleWebWorkflowError("SELENIUM_IMPORT_FAILED")
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    options = Options()
    options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
    try:
        return webdriver.Edge(options=options)
    except Exception as error:
        raise AppleWebWorkflowError("SELENIUM_EDGE_ATTACH_FAILED") from error


def _read_cdp_page_targets() -> tuple[dict[str, Any], ...]:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:9222/json/list", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        raise AppleWebWorkflowError("CDP_TARGET_LIST_FAILED") from error
    if not isinstance(payload, list):
        raise AppleWebWorkflowError("CDP_TARGET_LIST_INVALID")
    return tuple(
        item for item in payload if isinstance(item, dict) and item.get("type") == "page"
    )


def _create_cdp_page_target(url: str) -> None:
    request = urllib.request.Request(
        "http://127.0.0.1:9222/json/new?" + quote(url, safe=""),
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        raise AppleWebWorkflowError("CDP_TARGET_CREATE_FAILED") from error
    if not isinstance(payload, dict) or payload.get("type") != "page":
        raise AppleWebWorkflowError("CDP_TARGET_CREATE_INVALID")


def _ensure_selenium_page_target(
    *,
    target_reader: Callable[[], tuple[dict[str, Any], ...]] = _read_cdp_page_targets,
    target_creator: Callable[[str], None] = _create_cdp_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Mirror Fire_One's new-page pattern without restarting the existing Edge."""
    if target_reader():
        return
    target_creator(ACCOUNT_URL)
    for attempt in range(21):
        if attempt:
            sleeper(0.05)
        if target_reader():
            return
    raise AppleWebWorkflowError("CDP_TARGET_CREATE_UNVERIFIED")


def _wait_for_apple_login_handles(
    driver: Any,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Wait for a newly opened Developer Account page to reach Apple IDMS."""
    for attempt in range(21):
        if attempt:
            sleeper(0.05)
        matching_handles: list[str] = []
        for handle in tuple(driver.window_handles):
            driver.switch_to.window(handle)
            parsed = urlparse(driver.current_url)
            if (
                parsed.scheme == "https"
                and parsed.hostname == "idmsa.apple.com"
                and parsed.path.startswith("/IDMSWebAuth/signin")
            ):
                matching_handles.append(handle)
        if matching_handles:
            return matching_handles
    return []


def _prefer_current_handle(driver: Any, handles: list[str]) -> list[str]:
    if len(handles) <= 1:
        return handles
    current = getattr(driver, "current_window_handle", None)
    return [current] if current in handles else handles


def workflow_utm_10_fill_only_html(
    payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[str], dict[str, Any]]:
    """Inject only the email into the current Apple HTML form; never submit."""
    email = _required(payload, "APPLE_ACCOUNT_EMAIL")
    fill_password = payload.get("UTM_10_FILL_PASSWORD") is True
    check_remember_me = payload.get("UTM_10_CHECK_REMEMBER_ME") is True
    if check_remember_me and not fill_password:
        raise AppleWebWorkflowError("REMEMBER_ME_REQUIRES_FILL_PASSWORD")
    password = (
        _required(payload, "APPLE_ACCOUNT_PASSWORD") if fill_password else None
    )
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles = _wait_for_apple_login_handles(
            driver,
            sleeper=sleeper,
        )
        if not matching_handles:
            raise AppleWebWorkflowError(
                "SELENIUM_APPLE_LOGIN_PAGE_COUNT=0"
            )
        candidates: list[tuple[str, Any | None, int]] = []
        for handle in matching_handles:
            driver.switch_to.window(handle)
            driver.switch_to.default_content()
            contexts: list[Any | None] = [None]
            contexts.extend(driver.find_elements("css selector", "iframe,frame"))
            for context in contexts:
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                probe = driver.execute_script(HTML_EMAIL_PROBE_SCRIPT)
                if not isinstance(probe, dict) or probe.get("status") != "probed":
                    raise AppleWebWorkflowError("SELENIUM_EMAIL_PROBE_INVALID")
                count = probe.get("candidate_count")
                passwords = probe.get("password_count")
                if not isinstance(count, int) or not isinstance(passwords, int):
                    raise AppleWebWorkflowError("SELENIUM_EMAIL_PROBE_INVALID")
                if count:
                    candidates.append((handle, context, count))
        total_candidates = sum(count for _handle, _context, count in candidates)
        if total_candidates != 1 or len(candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_EMAIL_FIELD_COUNT={total_candidates}"
            )
        driver.switch_to.window(candidates[0][0])
        driver.switch_to.default_content()
        selected_context = candidates[0][1]
        if selected_context is not None:
            driver.switch_to.frame(selected_context)
        injected = driver.execute_script(HTML_EMAIL_INJECT_SCRIPT, email)
        if not isinstance(injected, dict):
            raise AppleWebWorkflowError("SELENIUM_EMAIL_INJECT_RESULT_INVALID")
        if injected.get("candidate_count") != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_EMAIL_FIELD_COUNT={injected.get('candidate_count')}"
            )
        if injected.get("status") != "injected":
            raise AppleWebWorkflowError("SELENIUM_EMAIL_INJECT_FAILED")
        verified = driver.execute_script(HTML_EMAIL_VERIFY_SCRIPT, email)
        if not isinstance(verified, dict):
            raise AppleWebWorkflowError("SELENIUM_EMAIL_READBACK_INVALID")
        if (
            verified.get("status") != "verified"
            or verified.get("candidate_count") != 1
            or verified.get("matches") is not True
        ):
            raise AppleWebWorkflowError("SELENIUM_EMAIL_READBACK_MISMATCH")
        if fill_password:
            clicked = driver.execute_script(HTML_EMAIL_CONTINUE_CLICK_SCRIPT)
            if (
                not isinstance(clicked, dict)
                or clicked.get("status") != "clicked"
                or clicked.get("candidate_count") != 1
            ):
                count = clicked.get("candidate_count") if isinstance(clicked, dict) else "INVALID"
                raise AppleWebWorkflowError(
                    f"SELENIUM_EMAIL_CONTINUE_COUNT={count}"
                )
            driver.switch_to.default_content()
            password_contexts: list[Any | None] = [None]
            password_contexts.extend(
                driver.find_elements("css selector", "iframe,frame")
            )
            password_candidates: list[tuple[Any | None, int]] = []
            for context in password_contexts:
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                probe = driver.execute_script(HTML_PASSWORD_PROBE_SCRIPT)
                if not isinstance(probe, dict) or probe.get("status") != "probed":
                    raise AppleWebWorkflowError("SELENIUM_PASSWORD_PROBE_INVALID")
                count = probe.get("candidate_count")
                if not isinstance(count, int):
                    raise AppleWebWorkflowError("SELENIUM_PASSWORD_PROBE_INVALID")
                if count:
                    password_candidates.append((context, count))
            total_passwords = sum(
                count for _context, count in password_candidates
            )
            if total_passwords != 1 or len(password_candidates) != 1:
                raise AppleWebWorkflowError(
                    f"SELENIUM_PASSWORD_FIELD_COUNT={total_passwords}"
                )
            driver.switch_to.default_content()
            password_context = password_candidates[0][0]
            if password_context is not None:
                driver.switch_to.frame(password_context)
            injected_password = driver.execute_script(
                HTML_PASSWORD_INJECT_SCRIPT, password
            )
            if (
                not isinstance(injected_password, dict)
                or injected_password.get("status") != "injected"
                or injected_password.get("candidate_count") != 1
            ):
                raise AppleWebWorkflowError("SELENIUM_PASSWORD_INJECT_FAILED")
            verified_password = driver.execute_script(
                HTML_PASSWORD_VERIFY_SCRIPT, password
            )
            if (
                not isinstance(verified_password, dict)
                or verified_password.get("status") != "verified"
                or verified_password.get("candidate_count") != 1
                or verified_password.get("matches") is not True
            ):
                raise AppleWebWorkflowError("SELENIUM_PASSWORD_READBACK_MISMATCH")
            if check_remember_me:
                remembered = driver.execute_script(HTML_REMEMBER_ME_CHECK_SCRIPT)
                if (
                    not isinstance(remembered, dict)
                    or remembered.get("status") != "checked"
                    or remembered.get("candidate_count") != 1
                    or remembered.get("checked") is not True
                ):
                    count = (
                        remembered.get("candidate_count")
                        if isinstance(remembered, dict)
                        else "INVALID"
                    )
                    raise AppleWebWorkflowError(
                        f"SELENIUM_REMEMBER_ME_COUNT={count}"
                    )
                remembered_readback = driver.execute_script(
                    HTML_REMEMBER_ME_VERIFY_SCRIPT
                )
                if (
                    not isinstance(remembered_readback, dict)
                    or remembered_readback.get("status") != "verified"
                    or remembered_readback.get("candidate_count") != 1
                    or remembered_readback.get("checked") is not True
                ):
                    raise AppleWebWorkflowError("SELENIUM_REMEMBER_ME_READBACK_FAILED")
        pid_after = pid_reuser()
        if pid_after != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        if fill_password:
            markers = [
                "EDGE_EXISTING_PID=verified",
                "EDGE_ACCOUNT_EMAIL_PAGE=verified",
                "APPLE_ACCOUNT_EMAIL_FIELD=verified",
                "EMAIL_CONTINUE_BUTTON=clicked",
                "APPLE_ACCOUNT_PASSWORD_PAGE=verified",
                "APPLE_ACCOUNT_PASSWORD_FIELD=verified",
            ]
            if check_remember_me:
                markers.append("REMEMBER_ME=checked")
            markers.extend(
                [
                    "PASSWORD_CONTINUE_BUTTON=not_clicked",
                    "UTM_10_FILL_CREDENTIALS=verified",
                ]
            )
            return markers, {"edge_pid": pid_before}
        return [
            "EDGE_EXISTING_PID=verified",
            "EDGE_ACCOUNT_EMAIL_PAGE=verified",
            "APPLE_ACCOUNT_EMAIL_FIELD=verified",
            "CONTINUE_BUTTON=not_clicked",
            "PASSWORD_FIELD=not_touched",
            "UTM_10_FILL_ONLY=verified",
        ], {"edge_pid": pid_before}
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def workflow_utm_10_remember_only_html(
    _payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[str], dict[str, Any]]:
    """Check Remember me on the current password page without credentials."""
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles: list[str] = []
        for handle in tuple(driver.window_handles):
            driver.switch_to.window(handle)
            parsed = urlparse(driver.current_url)
            if (
                parsed.scheme == "https"
                and parsed.hostname == "idmsa.apple.com"
                and parsed.path.startswith("/IDMSWebAuth/signin")
            ):
                matching_handles.append(handle)
        if len(matching_handles) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_APPLE_LOGIN_PAGE_COUNT={len(matching_handles)}"
            )
        driver.switch_to.window(matching_handles[0])
        driver.switch_to.default_content()
        contexts: list[Any | None] = [None]
        contexts.extend(driver.find_elements("css selector", "iframe,frame"))
        candidates: list[tuple[Any | None, int]] = []
        for context in contexts:
            driver.switch_to.default_content()
            if context is not None:
                driver.switch_to.frame(context)
            probe = driver.execute_script(HTML_PASSWORD_PROBE_SCRIPT)
            if not isinstance(probe, dict) or probe.get("status") != "probed":
                raise AppleWebWorkflowError("SELENIUM_PASSWORD_PROBE_INVALID")
            count = probe.get("candidate_count")
            if not isinstance(count, int):
                raise AppleWebWorkflowError("SELENIUM_PASSWORD_PROBE_INVALID")
            if count:
                candidates.append((context, count))
        total_passwords = sum(count for _context, count in candidates)
        if total_passwords != 1 or len(candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_FIELD_COUNT={total_passwords}"
            )
        driver.switch_to.default_content()
        selected_context = candidates[0][0]
        if selected_context is not None:
            driver.switch_to.frame(selected_context)
        remembered = driver.execute_script(HTML_REMEMBER_ME_CHECK_SCRIPT)
        if (
            not isinstance(remembered, dict)
            or remembered.get("status") != "checked"
            or remembered.get("candidate_count") != 1
            or remembered.get("checked") is not True
        ):
            count = remembered.get("candidate_count") if isinstance(remembered, dict) else "INVALID"
            raise AppleWebWorkflowError(f"SELENIUM_REMEMBER_ME_COUNT={count}")
        readback = driver.execute_script(HTML_REMEMBER_ME_VERIFY_SCRIPT)
        if (
            not isinstance(readback, dict)
            or readback.get("status") != "verified"
            or readback.get("candidate_count") != 1
            or readback.get("checked") is not True
        ):
            raise AppleWebWorkflowError("SELENIUM_REMEMBER_ME_READBACK_FAILED")
        pid_after = pid_reuser()
        if pid_after != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        return [
            "EDGE_EXISTING_PID=verified",
            "APPLE_ACCOUNT_PASSWORD_PAGE=verified",
            "REMEMBER_ME=checked",
            "PASSWORD_CONTINUE_BUTTON=not_clicked",
            "UTM_10_REMEMBER_ONLY=verified",
        ], {"edge_pid": pid_before}
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def _wait_for_unique_email_continue(
    driver: Any,
    *,
    context_index: int,
    switch_context: Callable[[int], None],
    sleeper: Callable[[float], None] = time.sleep,
    max_polls: int = 601,
) -> Any:
    """Wait up to 30 seconds for the unique visible Continue to enable."""
    visible: list[Any] = []
    for attempt in range(max_polls):
        if attempt:
            sleeper(0.05)
        switch_context(context_index)
        visible = []
        for element in driver.find_elements(
            "css selector", 'button,input[type="submit"]'
        ):
            label = " ".join(
                str(element.text or element.get_attribute("value") or "").split()
            )
            if label == "Continue" and element.is_displayed():
                visible.append(element)
        if (
            len(visible) == 1
            and visible[0].is_enabled()
            and visible[0].get_attribute("aria-disabled") != "true"
        ):
            return visible[0]
    raise AppleWebWorkflowError(
        f"SELENIUM_EMAIL_CONTINUE_COUNT={len(visible)}"
    )


def _submit_email_with_selenium(
    payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Submit one authoritative email through the unique IDMS DOM field."""
    email = _required(payload, "APPLE_ACCOUNT_EMAIL")
    selector = (
        "input#account_name_text_field,input[name=accountName],"
        "input[autocomplete=username],input[type=email],"
        'input[placeholder="Email or Phone Number"]'
    )
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles = _wait_for_apple_login_handles(driver, sleeper=sleeper)
        matching_handles = _apple_login_handles_with_email(
            driver, matching_handles
        )
        matching_handles = _prefer_current_handle(driver, matching_handles)
        if len(matching_handles) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_APPLE_EMAIL_PAGE_COUNT={len(matching_handles)}"
            )
        driver.switch_to.window(matching_handles[0])

        def switch_context(index: int) -> None:
            driver.switch_to.default_content()
            frames = list(driver.find_elements("css selector", "iframe,frame"))
            if index:
                if index > len(frames):
                    raise AppleWebWorkflowError("SELENIUM_EMAIL_FRAME_CHANGED")
                driver.switch_to.frame(frames[index - 1])

        locations: list[tuple[int, int]] = []
        driver.switch_to.default_content()
        contexts = [None, *driver.find_elements("css selector", "iframe,frame")]
        for index, context in enumerate(contexts):
            driver.switch_to.default_content()
            if context is not None:
                driver.switch_to.frame(context)
            matches = [
                element
                for element in driver.find_elements("css selector", selector)
                if element.is_displayed()
                and element.is_enabled()
                and element.get_attribute("aria-disabled") != "true"
            ]
            if matches:
                locations.append((index, len(matches)))
        total = sum(count for _index, count in locations)
        if total != 1 or len(locations) != 1:
            raise AppleWebWorkflowError(f"SELENIUM_EMAIL_FIELD_COUNT={total}")
        context_index = locations[0][0]

        def find_field() -> Any:
            switch_context(context_index)
            matches = [
                element
                for element in driver.find_elements("css selector", selector)
                if element.is_displayed()
                and element.is_enabled()
                and element.get_attribute("aria-disabled") != "true"
            ]
            if len(matches) != 1:
                raise AppleWebWorkflowError(
                    f"SELENIUM_EMAIL_FIELD_COUNT={len(matches)}"
                )
            return matches[0]

        field = find_field()
        field.click()
        field = find_field()
        if (
            driver.execute_script(
                "return document.activeElement===arguments[0]", field
            )
            is not True
        ):
            raise AppleWebWorkflowError("SELENIUM_EMAIL_FOCUS_UNVERIFIED")
        field.clear()
        field.send_keys(email)
        if field.get_attribute("value") != email:
            raise AppleWebWorkflowError("SELENIUM_EMAIL_READBACK_MISMATCH")
        continue_button = _wait_for_unique_email_continue(
            driver,
            context_index=context_index,
            switch_context=switch_context,
            sleeper=sleeper,
        )
        continue_button.click()

        password_ready = False
        for attempt in range(21):
            if attempt:
                sleeper(0.05)
            driver.switch_to.default_content()
            password_count = 0
            for context in [
                None,
                *driver.find_elements("css selector", "iframe,frame"),
            ]:
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                password_count += sum(
                    1
                    for element in driver.find_elements(
                        "css selector", 'input[type="password"]'
                    )
                    if element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                )
            if password_count == 1:
                password_ready = True
                break
        if not password_ready:
            raise AppleWebWorkflowError("SELENIUM_PASSWORD_FIELD_COUNT=0")
        if pid_reuser() != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
    except AppleWebWorkflowError:
        raise
    except Exception:
        raise AppleWebWorkflowError("SELENIUM_EMAIL_SUBMIT_FAILED") from None
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def _apple_login_handles_with_email(
    driver: Any, matching_handles: list[str]
) -> list[str]:
    email_handles: list[str] = []
    for handle in matching_handles:
        driver.switch_to.window(handle)
        driver.switch_to.default_content()
        contexts: list[Any | None] = [None]
        contexts.extend(driver.find_elements("css selector", "iframe,frame"))
        count = 0
        for context in contexts:
            driver.switch_to.default_content()
            if context is not None:
                driver.switch_to.frame(context)
            probe = driver.execute_script(HTML_EMAIL_PROBE_SCRIPT)
            if not isinstance(probe, dict) or probe.get("status") != "probed":
                raise AppleWebWorkflowError("SELENIUM_EMAIL_PROBE_INVALID")
            candidate_count = probe.get("candidate_count")
            if not isinstance(candidate_count, int):
                raise AppleWebWorkflowError("SELENIUM_EMAIL_PROBE_INVALID")
            count += candidate_count
        if count == 1:
            email_handles.append(handle)
    return email_handles


def _apple_login_handles_with_password(
    driver: Any, matching_handles: list[str]
) -> list[str]:
    password_handles: list[str] = []
    for handle in matching_handles:
        driver.switch_to.window(handle)
        driver.switch_to.default_content()
        contexts = list(driver.find_elements("css selector", "iframe,frame"))
        count = 0
        for context in contexts:
            driver.switch_to.default_content()
            driver.switch_to.frame(context)
            count += sum(
                1
                for element in driver.find_elements(
                    "css selector", 'input[type="password"]'
                )
                if element.is_displayed()
                and element.is_enabled()
                and element.get_attribute("aria-disabled") != "true"
            )
        if count == 1:
            password_handles.append(handle)
    return password_handles


def _submit_phone_with_selenium(
    payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Choose the unique text-message method matching the live phone suffix."""
    phone_digits = re.sub(r"\D", "", _required(payload, "APPLE_ACCOUNT_PHONE"))
    if len(phone_digits) < 2:
        raise AppleWebWorkflowError("PHONE_SUFFIX_INVALID")
    suffix = phone_digits[-2:]
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles = _wait_for_apple_login_handles(driver, sleeper=sleeper)
        matching_handles = _prefer_current_handle(driver, matching_handles)
        if len(matching_handles) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_APPLE_PHONE_PAGE_COUNT={len(matching_handles)}"
            )
        driver.switch_to.window(matching_handles[0])

        matches: list[tuple[int, Any]] = []
        for attempt in range(601):
            if attempt:
                sleeper(0.05)
            matches = []
            driver.switch_to.default_content()
            contexts = [None, *driver.find_elements("css selector", "iframe,frame")]
            for index, context in enumerate(contexts):
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                for element in driver.find_elements("css selector", "button"):
                    label = " ".join(
                        str(
                            element.text
                            or element.get_attribute("aria-label")
                            or ""
                        ).split()
                    )
                    digits = re.sub(r"\D", "", label)
                    if (
                        "text message" in label.casefold()
                        and digits.endswith(suffix)
                        and element.is_displayed()
                        and element.is_enabled()
                        and element.get_attribute("aria-disabled") != "true"
                    ):
                        matches.append((index, element))
            if len(matches) == 1:
                break
            if len(matches) > 1:
                raise AppleWebWorkflowError(
                    f"SELENIUM_PHONE_METHOD_COUNT={len(matches)}"
                )
        if len(matches) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PHONE_METHOD_COUNT={len(matches)}"
            )
        context_index, method = matches[0]
        driver.switch_to.default_content()
        frames = list(driver.find_elements("css selector", "iframe,frame"))
        if context_index:
            if context_index > len(frames):
                raise AppleWebWorkflowError("SELENIUM_PHONE_FRAME_CHANGED")
            driver.switch_to.frame(frames[context_index - 1])
        method.click()

        otp_ready = False
        for attempt in range(601):
            if attempt:
                sleeper(0.05)
            driver.switch_to.default_content()
            otp_count = 0
            for context in [
                None,
                *driver.find_elements("css selector", "iframe,frame"),
            ]:
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                otp_count += sum(
                    1
                    for element in driver.find_elements(
                        "css selector", 'input[type="tel"]'
                    )
                    if element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                )
            if otp_count in {1, 6}:
                otp_ready = True
                break
        if not otp_ready:
            raise AppleWebWorkflowError("SELENIUM_OTP_FIELD_COUNT=0")
        if pid_reuser() != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
    except AppleWebWorkflowError:
        raise
    except Exception:
        raise AppleWebWorkflowError("SELENIUM_PHONE_SUBMIT_FAILED") from None
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def _otp_readback_or_account(driver: Any, fields: list[Any], otp: str) -> bool:
    parsed = urlparse(driver.current_url)
    if parsed.hostname == "developer.apple.com" and parsed.path.startswith(
        "/account"
    ):
        return True
    current = "".join(str(field.get_attribute("value") or "") for field in fields)
    return current == otp


def _submit_otp_with_selenium(
    payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Fetch and enter the current OTP through the unique six DOM inputs."""
    otp = _fetch_otp(_required(payload, "APPLE_ACCOUNT_SMS_URL"))
    if not re.fullmatch(r"\d{6}", otp):
        raise AppleWebWorkflowError("OTP_VALUE_INVALID")
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles = _wait_for_apple_login_handles(driver, sleeper=sleeper)
        matching_handles = _prefer_current_handle(driver, matching_handles)
        if len(matching_handles) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_APPLE_OTP_PAGE_COUNT={len(matching_handles)}"
            )
        driver.switch_to.window(matching_handles[0])

        locations: list[tuple[int, list[Any]]] = []
        for attempt in range(601):
            if attempt:
                sleeper(0.05)
            locations = []
            driver.switch_to.default_content()
            contexts = [None, *driver.find_elements("css selector", "iframe,frame")]
            for index, context in enumerate(contexts):
                driver.switch_to.default_content()
                if context is not None:
                    driver.switch_to.frame(context)
                fields = [
                    element
                    for element in driver.find_elements(
                        "css selector", 'input[type="tel"]'
                    )
                    if element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                ]
                if fields:
                    locations.append((index, fields))
            total = sum(len(fields) for _index, fields in locations)
            if total == 6 and len(locations) == 1:
                break
        total = sum(len(fields) for _index, fields in locations)
        if total != 6 or len(locations) != 1:
            raise AppleWebWorkflowError(f"SELENIUM_OTP_FIELD_COUNT={total}")
        context_index, fields = locations[0]
        driver.switch_to.default_content()
        frames = list(driver.find_elements("css selector", "iframe,frame"))
        if context_index:
            if context_index > len(frames):
                raise AppleWebWorkflowError("SELENIUM_OTP_FRAME_CHANGED")
            driver.switch_to.frame(frames[context_index - 1])
        for field in fields:
            field.clear()
        fields[0].click()
        fields[0].send_keys(otp)

        verified = False
        for attempt in range(601):
            if attempt:
                sleeper(0.05)
            if _otp_readback_or_account(driver, fields, otp):
                verified = True
                break
        if not verified:
            raise AppleWebWorkflowError("SELENIUM_OTP_READBACK_MISMATCH")
        _complete_otp_trust_with_selenium(driver, sleeper=sleeper)
        if pid_reuser() != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
    except AppleWebWorkflowError:
        raise
    except Exception:
        raise AppleWebWorkflowError("SELENIUM_OTP_SUBMIT_FAILED") from None
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def _complete_otp_trust_with_selenium(
    driver: Any,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    max_polls: int = 1801,
) -> None:
    """Complete the optional unique Trust prompt and reach Developer Account."""
    trust_clicked = False
    for attempt in range(max_polls):
        if attempt:
            sleeper(0.05)
        try:
            parsed = urlparse(driver.current_url)
        except Exception:
            parsed = None
        if (
            parsed is not None
            and parsed.hostname == "developer.apple.com"
            and parsed.path.startswith("/account")
        ):
            return

        driver.switch_to.default_content()
        contexts = [None, *driver.find_elements("css selector", "iframe,frame")]
        trust_matches: list[tuple[int, Any]] = []
        for index, context in enumerate(contexts):
            driver.switch_to.default_content()
            if context is not None:
                driver.switch_to.frame(context)
            for element in driver.find_elements("css selector", "button"):
                label = " ".join(
                    str(
                        element.text
                        or element.get_attribute("aria-label")
                        or ""
                    ).split()
                )
                if (
                    label == "Trust"
                    and element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                ):
                    trust_matches.append((index, element))
        if len(trust_matches) > 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_TRUST_BUTTON_COUNT={len(trust_matches)}"
            )
        if len(trust_matches) == 1 and not trust_clicked:
            index, trust = trust_matches[0]
            driver.switch_to.default_content()
            frames = list(driver.find_elements("css selector", "iframe,frame"))
            if index:
                if index > len(frames):
                    raise AppleWebWorkflowError("SELENIUM_TRUST_FRAME_CHANGED")
                driver.switch_to.frame(frames[index - 1])
            trust.click()
            trust_clicked = True
    raise AppleWebWorkflowError("SELENIUM_DEVELOPER_ACCOUNT_TIMEOUT")


def _password_final_action_candidates(
    elements: list[Any], *, require_enabled: bool = True
) -> list[Any]:
    matches: list[Any] = []
    for element in elements:
        label = " ".join(
            str(element.text or element.get_attribute("value") or "").split()
        )
        if (
            label in {"Continue", "Sign In"}
            and element.is_displayed()
            and (
                not require_enabled
                or (
                    element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                )
            )
        ):
            matches.append(element)
    return matches


def _submit_password_with_selenium(
    payload: Mapping[str, Any],
    *,
    driver_factory: Callable[[], Any] = _attach_existing_edge_with_selenium,
    pid_reuser: Callable[[], int] = reuse_utm10_edge,
    target_preparer: Callable[[], None] = _ensure_selenium_page_target,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Submit the password through one exact HTML input in the existing Edge."""
    password = _required(payload, "APPLE_ACCOUNT_PASSWORD")
    pid_before = pid_reuser()
    driver = None
    try:
        target_preparer()
        driver = driver_factory()
        matching_handles: list[str] = []
        for handle in tuple(driver.window_handles):
            driver.switch_to.window(handle)
            parsed = urlparse(driver.current_url)
            if (
                parsed.scheme == "https"
                and parsed.hostname == "idmsa.apple.com"
                and parsed.path.startswith("/IDMSWebAuth/signin")
            ):
                matching_handles.append(handle)
        matching_handles = _apple_login_handles_with_password(
            driver, matching_handles
        )
        matching_handles = _prefer_current_handle(driver, matching_handles)
        if len(matching_handles) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_APPLE_PASSWORD_PAGE_COUNT={len(matching_handles)}"
            )

        driver.switch_to.window(matching_handles[0])
        driver.switch_to.default_content()
        frame_contexts = list(driver.find_elements("css selector", "iframe,frame"))
        password_candidates: list[tuple[Any, Any]] = []
        for context in frame_contexts:
            driver.switch_to.default_content()
            driver.switch_to.frame(context)
            for element in driver.find_elements(
                "css selector", 'input[type="password"]'
            ):
                if (
                    element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                ):
                    password_candidates.append((context, element))
        if len(password_candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_FIELD_COUNT={len(password_candidates)}"
            )

        all_contexts: list[Any | None] = [None, *frame_contexts]

        def find_continue_candidates(
            *, require_enabled: bool = True
        ) -> list[tuple[Any | None, Any]]:
            matches: list[tuple[Any | None, Any]] = []
            for candidate_context in all_contexts:
                driver.switch_to.default_content()
                if candidate_context is not None:
                    driver.switch_to.frame(candidate_context)
                for element in driver.find_elements(
                    "css selector",
                    'button,input[type="button"],input[type="submit"]',
                ):
                    if _password_final_action_candidates(
                        [element], require_enabled=require_enabled
                    ):
                        matches.append((candidate_context, element))
            return matches

        continue_candidates = find_continue_candidates(require_enabled=False)
        if len(continue_candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_CONTINUE_COUNT={len(continue_candidates)}"
            )

        context, password_element = password_candidates[0]
        if continue_candidates[0][0] is not context:
            raise AppleWebWorkflowError(
                "SELENIUM_PASSWORD_CONTINUE_CONTEXT_MISMATCH"
            )
        driver.switch_to.default_content()
        driver.switch_to.frame(context)
        password_element.click()

        driver.switch_to.default_content()
        frame_contexts = list(driver.find_elements("css selector", "iframe,frame"))
        password_candidates = []
        for candidate_context in frame_contexts:
            driver.switch_to.default_content()
            driver.switch_to.frame(candidate_context)
            for element in driver.find_elements(
                "css selector", 'input[type="password"]'
            ):
                if (
                    element.is_displayed()
                    and element.is_enabled()
                    and element.get_attribute("aria-disabled") != "true"
                ):
                    password_candidates.append((candidate_context, element))
        if len(password_candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_FIELD_COUNT={len(password_candidates)}"
            )
        context, password_element = password_candidates[0]
        all_contexts = [None, *frame_contexts]
        continue_candidates = find_continue_candidates(require_enabled=False)
        if len(continue_candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_CONTINUE_COUNT={len(continue_candidates)}"
            )
        if continue_candidates[0][0] is not context:
            raise AppleWebWorkflowError(
                "SELENIUM_PASSWORD_CONTINUE_CONTEXT_MISMATCH"
            )
        if pid_reuser() != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        driver.switch_to.default_content()
        driver.switch_to.frame(context)
        focused = driver.execute_script(
            "arguments[0].focus(); return document.activeElement === arguments[0];",
            password_element,
        )
        if focused is not True:
            raise AppleWebWorkflowError("SELENIUM_PASSWORD_FOCUS_UNVERIFIED")
        password_element.clear()
        password_element.send_keys(password)
        if password_element.get_attribute("value") != password:
            raise AppleWebWorkflowError("SELENIUM_PASSWORD_READBACK_MISMATCH")

        remembered = driver.execute_script(HTML_REMEMBER_ME_CHECK_SCRIPT)
        if (
            not isinstance(remembered, dict)
            or remembered.get("status") != "checked"
            or remembered.get("candidate_count") != 1
            or remembered.get("checked") is not True
        ):
            count = (
                remembered.get("candidate_count")
                if isinstance(remembered, dict)
                else "INVALID"
            )
            raise AppleWebWorkflowError(f"SELENIUM_REMEMBER_ME_COUNT={count}")
        remembered_readback = driver.execute_script(HTML_REMEMBER_ME_VERIFY_SCRIPT)
        if (
            not isinstance(remembered_readback, dict)
            or remembered_readback.get("status") != "verified"
            or remembered_readback.get("candidate_count") != 1
            or remembered_readback.get("checked") is not True
        ):
            raise AppleWebWorkflowError("SELENIUM_REMEMBER_ME_READBACK_FAILED")

        continue_candidates = find_continue_candidates()
        if len(continue_candidates) != 1:
            raise AppleWebWorkflowError(
                f"SELENIUM_PASSWORD_CONTINUE_COUNT={len(continue_candidates)}"
            )
        continue_context, continue_element = continue_candidates[0]
        if continue_context is not context:
            raise AppleWebWorkflowError(
                "SELENIUM_PASSWORD_CONTINUE_CONTEXT_MISMATCH"
            )
        driver.switch_to.default_content()
        driver.switch_to.frame(context)
        continue_element.click()
        if pid_reuser() != pid_before:
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
    except AppleWebWorkflowError:
        raise
    except Exception:
        raise AppleWebWorkflowError("SELENIUM_PASSWORD_SUBMIT_FAILED") from None
    finally:
        service = getattr(driver, "service", None)
        stop = getattr(service, "stop", None)
        if callable(stop):
            stop()


def _ensure_apple_login(
    backend: EdgeAXBackend,
    payload: Mapping[str, Any],
    *,
    email_submitter: Callable[[Mapping[str, Any]], None] | None = None,
    password_submitter: Callable[[Mapping[str, Any]], None] | None = None,
    phone_submitter: Callable[[Mapping[str, Any]], None] | None = None,
    otp_submitter: Callable[[Mapping[str, Any]], None] | None = None,
) -> None:
    initial_anchors = (
        "Sign in to Apple Developer",
        "Email or Phone Number",
        "Password",
        "Choose a phone number",
        "verification code",
        "Verify Your Identity",
        "Membership details",
    )
    try:
        state = backend.wait_for_text(initial_anchors, timeout=90)
    except Exception as error:
        if str(error).startswith("PAGE_TEXT_TIMEOUT="):
            raise AppleWebWorkflowError(
                "UTM10_LOGIN_STATE_TIMEOUT=" + backend.diagnostic_fingerprint()
            ) from error
        raise
    if state == "Membership details" or backend.has_text("Membership details"):
        return

    phone_state = "Choose a phone number"
    verification_states = {"verification code", "Verify Your Identity"}
    if state not in verification_states and (
        state in {"Sign in to Apple Developer", "Email or Phone Number"}
        or backend.has_text("Email or Phone Number")
    ):
        (email_submitter or _submit_email_with_selenium)(payload)
        state = backend.wait_for_text(
            (
                "Password",
                phone_state,
                "verification code",
                "Verify Your Identity",
                "Membership details",
            ),
            timeout=90,
        )

    if state not in verification_states and (
        state == "Password" or backend.has_text("Password")
    ):
        try:
            (password_submitter or _submit_password_with_selenium)(payload)
        except AppleWebWorkflowError as error:
            if str(error) != "SELENIUM_APPLE_PASSWORD_PAGE_COUNT=0":
                raise
            (email_submitter or _submit_email_with_selenium)(payload)
            state = backend.wait_for_text(
                (
                    "Password",
                    phone_state,
                    "verification code",
                    "Verify Your Identity",
                    "Membership details",
                ),
                timeout=90,
            )
            if state == "Password" or backend.has_text("Password"):
                (password_submitter or _submit_password_with_selenium)(payload)
        try:
            state = backend.wait_for_text(
                (
                    phone_state,
                    "verification code",
                    "Verify Your Identity",
                    "Membership details",
                ),
                timeout=90,
            )
        except Exception as error:
            if not str(error).startswith("PAGE_TEXT_TIMEOUT="):
                raise
            (phone_submitter or _submit_phone_with_selenium)(payload)
            try:
                state = backend.wait_for_text(
                    ("verification code", "Verify Your Identity", "Membership details"),
                    timeout=90,
                )
            except Exception as otp_error:
                if not str(otp_error).startswith("PAGE_TEXT_TIMEOUT="):
                    raise
                (otp_submitter or _submit_otp_with_selenium)(payload)
                return

    if state == phone_state:
        (phone_submitter or _submit_phone_with_selenium)(payload)
        state = backend.wait_for_text(
            ("verification code", "Verify Your Identity", "Membership details"),
            timeout=90,
        )

    if state in {"verification code", "Verify Your Identity"}:
        (otp_submitter or _submit_otp_with_selenium)(payload)
    backend.wait_for_text("Membership details", timeout=90)


def _accept_agreement_if_needed(
    backend: EdgeAXBackend,
    ledger: GuestAttemptLedger,
) -> None:
    if not backend.has_text("Review agreement"):
        ledger.complete("developer_agreement")
        return
    if not ledger.begin("developer_agreement"):
        raise AppleWebWorkflowError("DEVELOPER_AGREEMENT_AMBIGUOUS")
    backend.click_text("Review agreement")
    backend.wait_for_text(("Agreement", "Apple Developer Program License Agreement"))
    if backend.has_text("I have read and agree"):
        backend.set_checkbox("I have read and agree", True)
    backend.click_text("Agree", exact=True)
    backend.wait_for_text("Account")
    if backend.has_text("Review agreement"):
        raise AppleWebWorkflowError("AGREEMENT_ACCEPTANCE_UNVERIFIED")
    ledger.complete("developer_agreement")


def workflow_utm_10(backend: EdgeAXBackend, payload: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    started_pid = payload.get("UTM_10_EDGE_STARTED_PID")
    if (
        not isinstance(started_pid, int)
        or isinstance(started_pid, bool)
        or started_pid <= 0
        or backend.pid != started_pid
    ):
        raise AppleWebWorkflowError("EDGE_UTM10_PID_MISMATCH")
    reuse_existing = payload.get("UTM_10_REUSE_EXISTING_EDGE") is True
    if not reuse_existing:
        backend.open_url(ACCOUNT_URL)
    if (
        payload.get("UTM_10_HTML_FORMAL_FLOW") is True
        and not backend.has_text("Membership details")
    ):
        staged_payload = dict(payload)
        staged_payload["UTM_10_FILL_PASSWORD"] = True
        staged_payload["UTM_10_CHECK_REMEMBER_ME"] = True
        workflow_utm_10_fill_only_html(staged_payload)
        _submit_password_with_selenium(payload)
    _ensure_apple_login(backend, payload)
    backend.wait_for_text("Membership details")
    return [
        "EDGE_EXISTING_PID=verified",
        "EDGE_UTM10_CDP_STARTED=verified",
        "EDGE_ACCOUNT_PAGE=verified",
        "APPLE_ACCOUNT=verified",
        "UTM_10=verified",
    ], {"edge_pid": started_pid}


def _accept_paid_apps_agreement(backend: EdgeAXBackend) -> None:
    backend.open_url(BUSINESS_URL)
    backend.wait_for_text(("Business", "Agreements", "Paid Apps"), timeout=90)
    if not backend.has_text("Sign the Paid Apps Agreement"):
        return
    backend.click_text("Sign the Paid Apps Agreement")
    backend.wait_for_text("Paid Applications Agreement")
    if backend.has_text("I have read and agree"):
        backend.set_checkbox("I have read and agree", True)
    backend.click_text(("Agree", "Submit"), exact=True)
    backend.wait_for_text(("Business", "Agreements"), timeout=90)


def _open_small_business_enrollment(backend: EdgeAXBackend) -> None:
    if backend.has_text(SUCCESS_MESSAGE_1) and backend.has_text(SUCCESS_MESSAGE_2):
        return
    backend.open_url(SMALL_BUSINESS_URL)
    backend.wait_for_text(
        (
            "Enroll now",
            "Enroll in the App Store Small Business Program",
            SUCCESS_MESSAGE_1,
        ),
        timeout=90,
    )
    if backend.has_text(SUCCESS_MESSAGE_1) and backend.has_text(SUCCESS_MESSAGE_2):
        return
    if backend.has_text("Enroll now"):
        backend.click_text("Enroll now", exact=True)
        backend.wait_for_text(
            ("Enroll in the App Store Small Business Program", SUCCESS_MESSAGE_1),
            timeout=90,
        )
        return
    if backend.has_text("Enroll in the App Store Small Business Program"):
        return
    raise AppleWebWorkflowError("SMALL_BUSINESS_ENROLLMENT_PAGE_UNVERIFIED")


def workflow_utm_11(backend: EdgeAXBackend, payload: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    success_visible = backend.has_text(SUCCESS_MESSAGE_1) and backend.has_text(
        SUCCESS_MESSAGE_2
    )
    paid_apps_marker = (
        "PAID_APPS_AGREEMENT=verified_by_enrollment_success"
        if success_visible
        else "PAID_APPS_AGREEMENT=accepted"
    )
    if not success_visible:
        if payload.get("ALLOW_ENROLLMENT_SUBMIT") is not True:
            raise AppleWebWorkflowError("ENROLLMENT_SUBMIT_READ_ONLY_RECOVERY")
        _accept_paid_apps_agreement(backend)
        _open_small_business_enrollment(backend)
        success_visible = backend.has_text(SUCCESS_MESSAGE_1) and backend.has_text(
            SUCCESS_MESSAGE_2
        )
        if not success_visible:
            backend.wait_for_text("Enroll in the App Store Small Business Program")
            for question, answer in QUESTIONS:
                backend.select_radio(question, answer)
            backend.set_checkbox("To the best of your knowledge", True)
            backend.click_text("Submit", exact=True)
            backend.wait_for_text(SUCCESS_MESSAGE_1, timeout=120)
    if not (backend.has_text(SUCCESS_MESSAGE_1) and backend.has_text(SUCCESS_MESSAGE_2)):
        raise AppleWebWorkflowError("SMALL_BUSINESS_SUCCESS_UNVERIFIED")
    screenshot = f"/Users/{_required(payload, 'VM_USER')}/Downloads/05-small-business.png"
    backend.capture_png(screenshot)
    return [
        "EDGE_EXISTING_PID=verified",
        paid_apps_marker,
        "QUESTIONNAIRE=verified",
        "SMALL_BUSINESS_SUCCESS_MESSAGES=verified",
        "REVIEW_SCREENSHOT_05_GUEST=verified",
        "UTM_11=verified",
    ], {"screenshot_path": screenshot}


def _register_app_id(
    backend: EdgeAXBackend,
    app_name: str,
    bundle_id: str,
    ledger: GuestAttemptLedger,
) -> None:
    backend.open_url(IDENTIFIERS_URL)
    backend.wait_for_text(("Identifiers", "Certificates, Identifiers & Profiles"))
    if backend.has_text(bundle_id):
        ledger.complete("app_id_register")
        return
    if not ledger.begin("app_id_register"):
        raise AppleWebWorkflowError("APP_ID_REGISTER_AMBIGUOUS")
    backend.open_url(IDENTIFIER_ADD_URL)
    backend.wait_for_text(("Register an App ID", "Register a New Identifier"))
    if backend.has_text("App"):
        backend.click_text("App", exact=True, optional=True)
    backend.set_field("Description", app_name)
    backend.set_field("Bundle ID", bundle_id)
    backend.click_text("Continue", exact=True)
    backend.wait_for_text(("Confirm your App ID", bundle_id))
    backend.click_text("Register", exact=True)
    backend.wait_for_text(("Identifiers", bundle_id), timeout=90)
    if not backend.has_text(bundle_id):
        raise AppleWebWorkflowError("APP_ID_REGISTER_UNVERIFIED")
    ledger.complete("app_id_register")


def _create_app(
    backend: EdgeAXBackend,
    app_name: str,
    bundle_id: str,
    ledger: GuestAttemptLedger,
) -> str:
    backend.open_url(APPS_URL)
    backend.wait_for_text(("Apps", "My Apps"), timeout=90)
    if backend.has_text(app_name):
        backend.click_text(app_name, exact=True)
        backend.wait_for_text(("iOS App Version 1.0", "Distribution"), timeout=90)
        ledger.complete("app_create")
        return "existing_exact"
    if not ledger.begin("app_create"):
        raise AppleWebWorkflowError("APP_CREATE_AMBIGUOUS")
    backend.click_text(("Add Apps", "New App"))
    backend.wait_for_text("New App")
    if backend.has_text("iOS"):
        backend.set_checkbox("iOS", True)
    backend.set_field("Name", app_name)
    backend.select_option("Primary Language", "English (U.S.)")
    backend.select_option("Bundle ID", bundle_id)
    backend.set_field("SKU", bundle_id)
    backend.select_option("User Access", "Full Access")
    backend.click_text("Create", exact=True)
    backend.wait_for_text(("iOS App Version 1.0", "Distribution"), timeout=120)
    ledger.complete("app_create")
    return "created"


def workflow_utm_12(backend: EdgeAXBackend, payload: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    app_name = _required(payload, "APP_NAME")
    bundle_id = _required(payload, "BUNDLE_ID")
    phase = _required(payload, "UTM_12_PHASE")
    if phase not in {"membership", "apps"}:
        raise AppleWebWorkflowError("UTM_12_PHASE_INVALID")
    ledger = GuestAttemptLedger(
        run_id=_required(payload, "RUN_ID"),
        vm_name=_required(payload, "VM_NAME"),
        identity={"app_name": app_name, "bundle_id": bundle_id},
    )
    if phase == "membership":
        backend.open_url(ACCOUNT_URL)
        backend.wait_for_text("Membership details")
        team_id = backend.read_value_after_label("Team ID")
        renewal_date = backend.read_value_after_label("Renewal date")
        return [
            "EDGE_EXISTING_PID=verified",
            "DEVELOPER_ACCOUNT=opened",
            "MEMBERSHIP_DETAILS=verified",
        ], {
            "team_id": team_id,
            "renewal_date": renewal_date,
            "agreement_attempt_id": ledger.attempt_id("developer_agreement"),
            "edge_pid": backend.pid,
        }

    expected_edge_pid = payload.get("EXPECTED_EDGE_PID")
    if (
        not isinstance(expected_edge_pid, int)
        or isinstance(expected_edge_pid, bool)
        or expected_edge_pid <= 0
        or backend.pid != expected_edge_pid
    ):
        raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
    _register_app_id(backend, app_name, bundle_id, ledger)
    app_state = _create_app(backend, app_name, bundle_id, ledger)
    return [
        "EDGE_EXISTING_PID=verified",
        "APP_ID_REGISTERED=verified",
        "APP_STORE_APP=created_or_existing_exact",
        "UTM_12=verified",
    ], {
        "app_state": app_state,
        "app_id_register_attempt_id": ledger.attempt_id("app_id_register"),
        "app_create_attempt_id": ledger.attempt_id("app_create"),
    }


@dataclass(frozen=True)
class CsrEvidence:
    path: str
    size: int
    sha256: str
    public_key_sha256: str


@dataclass(frozen=True)
class CertificateEvidence:
    path: str
    size: int
    sha256: str
    fingerprint_sha1: str
    fingerprint_sha256: str
    public_key_sha256: str
    subject: str
    not_before: str
    not_after: str


@dataclass(frozen=True)
class SigningIdentity:
    sha1: str
    common_name: str


class StableUtm13Ledger:
    """Mode-600 one-shot ledger for certificate/profile irreversible actions."""

    STATES = {"planned", "clicking", "unknown", "succeeded"}

    def __init__(
        self,
        *,
        run_id: str,
        vm_name: str,
        kind: str,
        details: Mapping[str, Any],
        state_root: Path | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", run_id):
            raise AppleWebWorkflowError("RUN_ID_INVALID")
        if not re.fullmatch(r"[a-z]{4}", vm_name):
            raise AppleWebWorkflowError("VM_NAME_INVALID")
        if kind not in {"certificate", "profile"}:
            raise AppleWebWorkflowError("UTM13_LEDGER_KIND_INVALID")
        root = state_root or Path(
            f"/Users/{vm_name}/Downloads/utm-apple-web-state/utm-13"
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.root = root
        self.path = root / f"{run_id}-{kind}.json"
        details_json = json.dumps(
            dict(details), sort_keys=True, separators=(",", ":")
        )
        details_sha256 = hashlib.sha256(details_json.encode("utf-8")).hexdigest()
        attempt_id = f"{kind}-{details_sha256[:24]}"
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise AppleWebWorkflowError("UTM13_LEDGER_UNSAFE")
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                self.state.get("kind") != kind
                or self.state.get("details_sha256") != details_sha256
                or self.state.get("attempt_id") != attempt_id
                or self.state.get("status") not in self.STATES
            ):
                raise AppleWebWorkflowError("UTM13_LEDGER_IDENTITY_MISMATCH")
        else:
            self.state = {
                "version": 1,
                "kind": kind,
                "attempt_id": attempt_id,
                "details_sha256": details_sha256,
                "status": "planned",
                "evidence": {},
            }
            self._save()

    def _save(self) -> None:
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(self.state, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)
        self.path.chmod(0o600)

    @property
    def attempt_id(self) -> str:
        return str(self.state["attempt_id"])

    @property
    def status(self) -> str:
        return str(self.state["status"])

    @property
    def evidence(self) -> Mapping[str, Any]:
        value = self.state.get("evidence")
        return value if isinstance(value, dict) else {}

    def begin(self) -> None:
        if self.status != "planned":
            raise AppleWebWorkflowError(f"UTM13_ATTEMPT_NOT_PLANNED={self.status.upper()}")
        self.state["status"] = "clicking"
        self._save()

    def mark_unknown(self) -> None:
        if self.status not in {"clicking", "unknown"}:
            raise AppleWebWorkflowError("UTM13_ATTEMPT_UNKNOWN_TRANSITION_INVALID")
        self.state["status"] = "unknown"
        self._save()

    def succeed(self, evidence: Mapping[str, Any]) -> None:
        if self.status not in {"planned", "clicking", "unknown", "succeeded"}:
            raise AppleWebWorkflowError("UTM13_ATTEMPT_SUCCESS_TRANSITION_INVALID")
        self.state["status"] = "succeeded"
        self.state["evidence"] = dict(evidence)
        self._save()


def _run_binary(
    command: list[str], *, input_bytes: bytes | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _public_key_sha256(pem: bytes) -> str:
    converted = _run_binary(
        ["/usr/bin/openssl", "pkey", "-pubin", "-outform", "DER"],
        input_bytes=pem,
    )
    if converted.returncode != 0 or not converted.stdout:
        raise AppleWebWorkflowError("PUBLIC_KEY_PARSE_FAILED")
    return hashlib.sha256(converted.stdout).hexdigest()


def _validate_csr_once(path: Path) -> CsrEvidence:
    if path.is_symlink() or not path.is_file():
        raise AppleWebWorkflowError("CSR_FILE_INVALID")
    size = path.stat().st_size
    if not 256 <= size <= 64 * 1024:
        raise AppleWebWorkflowError("CSR_SIZE_INVALID")
    verified = _run_binary(
        ["/usr/bin/openssl", "req", "-in", str(path), "-noout", "-verify"]
    )
    public_key = _run_binary(
        ["/usr/bin/openssl", "req", "-in", str(path), "-noout", "-pubkey"]
    )
    if verified.returncode != 0 or public_key.returncode != 0:
        raise AppleWebWorkflowError("CSR_VERIFY_FAILED")
    payload = path.read_bytes()
    return CsrEvidence(
        path=str(path),
        size=size,
        sha256=hashlib.sha256(payload).hexdigest(),
        public_key_sha256=_public_key_sha256(public_key.stdout),
    )


def _verify_csr_evidence(vm_user: str) -> CsrEvidence:
    path = Path(f"/Users/{vm_user}/Desktop/CertificateSigningRequest.certSigningRequest")
    first = _validate_csr_once(path)
    second = _validate_csr_once(path)
    if first != second:
        raise AppleWebWorkflowError("CSR_EVIDENCE_UNSTABLE")
    return second


def _verify_csr(vm_user: str) -> str:
    """Compatibility wrapper returning the stable CSR file SHA-256."""
    return _verify_csr_evidence(vm_user).sha256


def _openssl_certificate(path: Path, *arguments: str) -> tuple[str, bytes]:
    for certificate_format in ("DER", "PEM"):
        result = _run_binary(
            [
                "/usr/bin/openssl",
                "x509",
                "-inform",
                certificate_format,
                "-in",
                str(path),
                *arguments,
            ]
        )
        if result.returncode == 0:
            return certificate_format, result.stdout
    raise AppleWebWorkflowError("CERTIFICATE_X509_INVALID")


def _parse_certificate_time(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError as error:
        raise AppleWebWorkflowError("CERTIFICATE_DATE_INVALID") from error
    return parsed.replace(tzinfo=timezone.utc)


def _validate_certificate(
    path: Path, *, csr: CsrEvidence, team_id: str
) -> CertificateEvidence:
    if path.is_symlink() or not path.is_file():
        raise AppleWebWorkflowError("CERTIFICATE_FILE_INVALID")
    size = path.stat().st_size
    if not 256 <= size <= 1024 * 1024:
        raise AppleWebWorkflowError("CERTIFICATE_SIZE_INVALID")
    certificate_format, details_bytes = _openssl_certificate(
        path,
        "-noout",
        "-subject",
        "-nameopt",
        "RFC2253",
        "-dates",
        "-fingerprint",
        "-sha256",
    )
    details = details_bytes.decode("utf-8", errors="replace")
    subject_match = re.search(r"^subject=(.+)$", details, re.MULTILINE)
    before_match = re.search(r"^notBefore=(.+)$", details, re.MULTILINE)
    after_match = re.search(r"^notAfter=(.+)$", details, re.MULTILINE)
    fingerprint_match = re.search(
        r"Fingerprint=([A-Fa-f0-9:]+)", details, re.IGNORECASE
    )
    if not all((subject_match, before_match, after_match, fingerprint_match)):
        raise AppleWebWorkflowError("CERTIFICATE_METADATA_INCOMPLETE")
    subject = subject_match.group(1).strip()
    if "Apple Distribution:" not in subject:
        raise AppleWebWorkflowError("CERTIFICATE_TYPE_MISMATCH")
    visible_teams = set(re.findall(r"\(([A-Z0-9]{10})\)", subject))
    if visible_teams != {team_id}:
        raise AppleWebWorkflowError("CERTIFICATE_TEAM_MISMATCH")
    not_before = _parse_certificate_time(before_match.group(1))
    not_after = _parse_certificate_time(after_match.group(1))
    now = datetime.now(timezone.utc)
    if not_before > now or not_after <= now:
        raise AppleWebWorkflowError("CERTIFICATE_NOT_CURRENT")
    _format, public_key = _openssl_certificate(path, "-pubkey", "-noout")
    public_key_sha256 = _public_key_sha256(public_key)
    if public_key_sha256 != csr.public_key_sha256:
        raise AppleWebWorkflowError("CERTIFICATE_CSR_PUBLIC_KEY_MISMATCH")
    _format, sha1_bytes = _openssl_certificate(
        path, "-noout", "-fingerprint", "-sha1"
    )
    sha1_match = re.search(
        rb"Fingerprint=([A-Fa-f0-9:]+)", sha1_bytes, re.IGNORECASE
    )
    if not sha1_match:
        raise AppleWebWorkflowError("CERTIFICATE_SHA1_MISSING")
    return CertificateEvidence(
        path=str(path),
        size=size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        fingerprint_sha1=sha1_match.group(1).decode().replace(":", "").upper(),
        fingerprint_sha256=fingerprint_match.group(1).replace(":", "").upper(),
        public_key_sha256=public_key_sha256,
        subject=subject,
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
    )


def _keychain_fingerprints(keychain: Path) -> set[str]:
    found = _run_binary(
        ["/usr/bin/security", "find-certificate", "-a", "-p", str(keychain)]
    )
    if found.returncode != 0 and not found.stdout.strip():
        return set()
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----",
        found.stdout,
    )
    fingerprints: set[str] = set()
    for block in blocks:
        fingerprint = _run_binary(
            ["/usr/bin/openssl", "x509", "-noout", "-fingerprint", "-sha256"],
            input_bytes=block,
        )
        match = re.search(
            rb"Fingerprint=([A-Fa-f0-9:]+)", fingerprint.stdout, re.IGNORECASE
        )
        if fingerprint.returncode != 0 or not match:
            raise AppleWebWorkflowError("KEYCHAIN_CERTIFICATE_PARSE_FAILED")
        fingerprints.add(match.group(1).decode().replace(":", "").upper())
    return fingerprints


def _verify_keychain_certificate_twice(
    keychain: Path, fingerprint_sha256: str
) -> None:
    samples = (
        fingerprint_sha256 in _keychain_fingerprints(keychain),
        fingerprint_sha256 in _keychain_fingerprints(keychain),
    )
    if samples != (True, True):
        raise AppleWebWorkflowError("KEYCHAIN_CERTIFICATE_READBACK_FAILED")


def _import_exact_certificate(
    certificate: CertificateEvidence,
    *,
    vm_user: str,
    vm_password: str,
    authorization_attempt_id: str,
    actual_attempt_id: str,
) -> None:
    if authorization_attempt_id not in {"current-run", actual_attempt_id}:
        raise AppleWebWorkflowError("SYSTEM_KEYCHAIN_ATTEMPT_MISMATCH")
    certificate_path = Path(certificate.path)
    system_keychain = Path("/Library/Keychains/System.keychain")
    login_keychain = Path(f"/Users/{vm_user}/Library/Keychains/login.keychain-db")
    if not system_keychain.is_file() or not login_keychain.is_file():
        raise AppleWebWorkflowError("KEYCHAIN_PATH_INVALID")
    if certificate.fingerprint_sha256 not in _keychain_fingerprints(system_keychain):
        import_args = [
            "/usr/bin/security",
            "import",
            str(certificate_path),
            "-k",
            str(system_keychain),
            "-T",
            "/usr/bin/codesign",
        ]
        imported = _run_binary(["/usr/bin/sudo", "-n", *import_args])
        if imported.returncode != 0:
            password_bytes = (vm_password + "\n").encode("utf-8")
            try:
                imported = _run_binary(
                    ["/usr/bin/sudo", "-S", "-p", "", *import_args],
                    input_bytes=password_bytes,
                )
            finally:
                password_bytes = b""
        if (
            imported.returncode != 0
            and certificate.fingerprint_sha256
            not in _keychain_fingerprints(system_keychain)
        ):
            raise AppleWebWorkflowError("SYSTEM_KEYCHAIN_IMPORT_FAILED")
    _verify_keychain_certificate_twice(
        system_keychain, certificate.fingerprint_sha256
    )
    if certificate.fingerprint_sha256 not in _keychain_fingerprints(login_keychain):
        imported = _run_binary(
            [
                "/usr/bin/security",
                "import",
                str(certificate_path),
                "-k",
                str(login_keychain),
                "-T",
                "/usr/bin/codesign",
            ]
        )
        if (
            imported.returncode != 0
            and certificate.fingerprint_sha256
            not in _keychain_fingerprints(login_keychain)
        ):
            raise AppleWebWorkflowError("LOGIN_KEYCHAIN_IMPORT_FAILED")
    _verify_keychain_certificate_twice(login_keychain, certificate.fingerprint_sha256)


def _identity_inventory(team_id: str) -> tuple[SigningIdentity, ...]:
    result = subprocess.run(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AppleWebWorkflowError("CODESIGN_IDENTITY_READ_FAILED")
    identities: list[SigningIdentity] = []
    for line in result.stdout.splitlines():
        match = re.match(r'^\s*\d+\)\s+([A-Fa-f0-9]{40})\s+"([^"]+)"', line)
        if not match:
            continue
        sha1, common_name = match.groups()
        if common_name.startswith("Apple Distribution:") and f"({team_id})" in common_name:
            identities.append(SigningIdentity(sha1.upper(), common_name))
    return tuple(sorted(identities, key=lambda item: item.sha1))


def _stable_identity_inventory(team_id: str) -> tuple[SigningIdentity, ...]:
    first = _identity_inventory(team_id)
    second = _identity_inventory(team_id)
    if first != second:
        raise AppleWebWorkflowError("CODESIGN_IDENTITY_UNSTABLE")
    if len(second) > 1:
        raise AppleWebWorkflowError(f"CODESIGN_IDENTITY_COUNT={len(second)}")
    return second


def _identity_count(team_id: str | None = None) -> int:
    if team_id is None:
        result = subprocess.run(
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise AppleWebWorkflowError("CODESIGN_IDENTITY_READ_FAILED")
        return len(re.findall(r'"Apple Distribution:', result.stdout))
    return len(_identity_inventory(team_id))


def _stable_identity_count(team_id: str | None = None) -> int:
    if team_id is not None:
        return len(_stable_identity_inventory(team_id))
    counts = (_identity_count(), _identity_count())
    if counts[0] != counts[1]:
        raise AppleWebWorkflowError("CODESIGN_IDENTITY_UNSTABLE")
    return counts[0]


def _stable_page_text(backend: EdgeAXBackend) -> str:
    first = backend.page_text()
    second = backend.page_text()
    if first != second:
        raise AppleWebWorkflowError("APPLE_PAGE_STATE_UNSTABLE")
    return second


def _date_in_future(value: str) -> bool:
    for match in re.findall(
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2}",
        value,
        flags=re.IGNORECASE,
    ):
        for date_format in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                parsed = datetime.strptime(match, date_format).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if parsed > datetime.now(timezone.utc):
                return True
    return False


def _certificate_candidate_blocks(page_text: str) -> tuple[str, ...]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    indices = [
        index
        for index, line in enumerate(lines)
        if "apple distribution" in line.casefold()
    ]
    groups: list[list[int]] = []
    for index in indices:
        if groups and index - groups[-1][-1] <= 6:
            groups[-1].append(index)
        else:
            groups.append([index])
    blocks: list[str] = []
    for group in groups:
        block = " ".join(
            lines[max(0, group[0] - 3) : min(len(lines), group[-1] + 5)]
        )
        folded = block.casefold()
        if "revoked" in folded or "expired" in folded:
            continue
        if re.search(r"\b(active|valid)\b", folded) or _date_in_future(block):
            blocks.append(re.sub(r"\s+", " ", block).strip())
    return tuple(dict.fromkeys(blocks))


def _verify_visible_team_id(page_text: str, expected_team_id: str) -> None:
    visible = set(re.findall(r"\s-\s([A-Z0-9]{10})\b", page_text))
    if len(visible) > 1:
        raise AppleWebWorkflowError("CERTIFICATES_PAGE_TEAM_AMBIGUOUS")
    if visible and visible != {expected_team_id}:
        raise AppleWebWorkflowError("CERTIFICATES_PAGE_TEAM_MISMATCH")


def _certificate_evidence_path(
    ledger: StableUtm13Ledger, certificate: CertificateEvidence
) -> Path:
    directory = ledger.root / "certificate-downloads"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / f"{ledger.attempt_id}-{certificate.sha256[:24]}.cer"
    if target.exists():
        existing = target.read_bytes()
        if hashlib.sha256(existing).hexdigest() != certificate.sha256:
            raise AppleWebWorkflowError("CERTIFICATE_EVIDENCE_CONFLICT")
    else:
        shutil.copyfile(certificate.path, target)
    target.chmod(0o600)
    return target


def _expose_distribution_certificate(
    certificate: CertificateEvidence, vm_user: str
) -> None:
    target = Path(f"/Users/{vm_user}/Downloads/distribution.cer")
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise AppleWebWorkflowError("VISIBLE_CERTIFICATE_PATH_UNSAFE")
        if hashlib.sha256(target.read_bytes()).hexdigest() != certificate.sha256:
            raise AppleWebWorkflowError("VISIBLE_CERTIFICATE_CONFLICT")
    elif Path(certificate.path) != target:
        shutil.copyfile(certificate.path, target)
    target.chmod(0o600)


def _matching_certificate_files(
    paths: list[Path], *, csr: CsrEvidence, team_id: str
) -> list[CertificateEvidence]:
    matches: list[CertificateEvidence] = []
    for path in paths:
        try:
            evidence = _validate_certificate(path, csr=csr, team_id=team_id)
        except Exception:
            continue
        matches.append(evidence)
    hashes = {item.sha256 for item in matches}
    if len(hashes) > 1:
        raise AppleWebWorkflowError("MULTIPLE_CSR_MATCHING_CERTIFICATES")
    return sorted(matches, key=lambda item: item.path)


def _download_certificate(
    backend: EdgeAXBackend,
    *,
    vm_user: str,
    csr: CsrEvidence,
    team_id: str,
    ledger: StableUtm13Ledger,
) -> CertificateEvidence:
    downloads = Path(f"/Users/{vm_user}/Downloads")
    before = {
        path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in downloads.glob("*.cer")
        if path.is_file() and not path.is_symlink()
    }
    backend.wait_for_text("Download")
    backend.click_text("Download", exact=True)
    deadline = time.monotonic() + 60
    changed: list[Path] = []
    while time.monotonic() < deadline:
        changed = []
        for path in downloads.glob("*.cer"):
            if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                continue
            resolved = path.resolve()
            fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)
            if before.get(resolved) != fingerprint:
                changed.append(resolved)
        matches = _matching_certificate_files(changed, csr=csr, team_id=team_id)
        if matches:
            selected = matches[0]
            evidence_path = _certificate_evidence_path(ledger, selected)
            stored = _validate_certificate(evidence_path, csr=csr, team_id=team_id)
            _expose_distribution_certificate(stored, vm_user)
            return stored
        time.sleep(0.01)
    raise AppleWebWorkflowError("CERTIFICATE_DOWNLOAD_UNVERIFIED")


def _recover_certificate_evidence(
    ledger: StableUtm13Ledger, *, csr: CsrEvidence, team_id: str, vm_user: str
) -> CertificateEvidence | None:
    declared = ledger.evidence.get("certificate_path")
    candidates: list[Path] = []
    if isinstance(declared, str):
        candidate = Path(declared)
        if candidate.parent == ledger.root / "certificate-downloads":
            candidates.append(candidate)
    evidence_dir = ledger.root / "certificate-downloads"
    if evidence_dir.is_dir() and not evidence_dir.is_symlink():
        candidates.extend(evidence_dir.glob(f"{ledger.attempt_id}-*.cer"))
    matches = _matching_certificate_files(
        list(dict.fromkeys(candidates)), csr=csr, team_id=team_id
    )
    if not matches:
        return None
    selected = matches[0]
    _expose_distribution_certificate(selected, vm_user)
    return selected


def _create_distribution_certificate(
    backend: EdgeAXBackend,
    *,
    csr: CsrEvidence,
    ledger: StableUtm13Ledger,
) -> None:
    backend.open_url(CERTIFICATE_ADD_URL)
    backend.wait_for_text(("Create a New Certificate", "Create a certificate"))
    backend.click_text("Apple Distribution", exact=True)
    backend.click_text("Continue", exact=True)
    backend.choose_file("Choose File", csr.path)
    ledger.begin()
    try:
        backend.click_text("Continue", exact=True)
    except Exception:
        ledger.mark_unknown()
        raise
    try:
        backend.wait_for_text("Download", timeout=120)
    except Exception:
        ledger.mark_unknown()
        raise AppleWebWorkflowError("CERTIFICATE_CREATE_RESULT_UNKNOWN") from None


def _ensure_distribution_certificate(
    backend: EdgeAXBackend,
    *,
    vm_user: str,
    vm_password: str,
    team_id: str,
    csr: CsrEvidence,
    ledger: StableUtm13Ledger,
    authorization_attempt_id: str,
) -> tuple[CertificateEvidence, SigningIdentity, str]:
    created_this_attempt = False
    recovered = _recover_certificate_evidence(
        ledger, csr=csr, team_id=team_id, vm_user=vm_user
    )
    page_text = _stable_page_text(backend)
    _verify_visible_team_id(page_text, team_id)
    candidates = _certificate_candidate_blocks(page_text)
    download_ready = backend.has_text("Download")
    if len(candidates) > 1:
        raise AppleWebWorkflowError("MULTIPLE_ACTIVE_DISTRIBUTION_CERTIFICATES")
    if recovered is None:
        if ledger.status != "planned":
            if download_ready:
                pass
            elif len(candidates) == 1:
                backend.click_text("Apple Distribution", exact=True)
            else:
                raise AppleWebWorkflowError("CERTIFICATE_ATTEMPT_READ_ONLY_BLOCKED")
        elif len(candidates) == 1:
            backend.click_text("Apple Distribution", exact=True)
        else:
            _create_distribution_certificate(backend, csr=csr, ledger=ledger)
            created_this_attempt = True
        try:
            recovered = _download_certificate(
                backend,
                vm_user=vm_user,
                csr=csr,
                team_id=team_id,
                ledger=ledger,
            )
        except Exception:
            if ledger.status == "clicking":
                ledger.mark_unknown()
            raise
    evidence_path = _certificate_evidence_path(ledger, recovered)
    certificate = _validate_certificate(evidence_path, csr=csr, team_id=team_id)
    _import_exact_certificate(
        certificate,
        vm_user=vm_user,
        vm_password=vm_password,
        authorization_attempt_id=authorization_attempt_id,
        actual_attempt_id=ledger.attempt_id,
    )
    identities = _stable_identity_inventory(team_id)
    if len(identities) != 1 or identities[0].sha1 != certificate.fingerprint_sha1:
        raise AppleWebWorkflowError("CODESIGN_IDENTITY_CERTIFICATE_MISMATCH")
    ledger.succeed(
        {
            "certificate_path": str(evidence_path),
            "certificate_sha256": certificate.sha256,
            "fingerprint_sha256": certificate.fingerprint_sha256,
            "identity_sha1": identities[0].sha1,
        }
    )
    status = "installed" if created_this_attempt else "recovered"
    return certificate, identities[0], status


def _profile_blocks(
    page_text: str, *, app_name: str, bundle_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    exact: list[str] = []
    conflicts: list[str] = []
    for index, line in enumerate(lines):
        block = " ".join(lines[max(0, index - 3) : index + 6])
        normalized = re.sub(r"\s+", " ", block).strip()
        if line == app_name and re.search(r"App Store(?: Connect)?", block, re.I):
            exact.append(normalized)
        if bundle_id in block and app_name not in block:
            conflicts.append(normalized)
    return tuple(dict.fromkeys(exact)), tuple(dict.fromkeys(conflicts))


def _verify_profile_detail(
    backend: EdgeAXBackend,
    *,
    app_name: str,
    bundle_id: str,
    team_id: str,
    identity: SigningIdentity,
) -> None:
    page_text = _stable_page_text(backend)
    lines = {line.strip() for line in page_text.splitlines() if line.strip()}
    if app_name not in lines:
        raise AppleWebWorkflowError("PROFILE_NAME_UNVERIFIED")
    if bundle_id not in page_text:
        raise AppleWebWorkflowError("PROFILE_BUNDLE_ID_UNVERIFIED")
    if team_id not in page_text:
        raise AppleWebWorkflowError("PROFILE_TEAM_ID_UNVERIFIED")
    if not re.search(r"App Store(?: Connect)?", page_text, re.IGNORECASE):
        raise AppleWebWorkflowError("PROFILE_TYPE_UNVERIFIED")
    identity_name = re.sub(r"^Apple Distribution:\s*", "", identity.common_name)
    if identity.common_name not in page_text and identity_name not in page_text:
        raise AppleWebWorkflowError("PROFILE_CERTIFICATE_UNVERIFIED")
    if "Download and Install" not in page_text and "Download" not in lines:
        raise AppleWebWorkflowError("PROFILE_DOWNLOAD_UNAVAILABLE")


def _generate_profile(
    backend: EdgeAXBackend,
    *,
    app_name: str,
    bundle_id: str,
    team_id: str,
    identity: SigningIdentity,
    certificate: CertificateEvidence,
    ledger: StableUtm13Ledger,
) -> str:
    backend.open_url(PROFILES_URL)
    backend.wait_for_text(("Profiles", "Provisioning Profiles"))
    if backend.has_text("Download and Install"):
        _verify_profile_detail(
            backend,
            app_name=app_name,
            bundle_id=bundle_id,
            team_id=team_id,
            identity=identity,
        )
        ledger.succeed(
            {
                "profile_state": "existing_exact",
                "certificate_sha256": certificate.sha256,
            }
        )
        return "existing_exact"
    exact, conflicts = _profile_blocks(
        _stable_page_text(backend), app_name=app_name, bundle_id=bundle_id
    )
    if conflicts:
        raise AppleWebWorkflowError("PROFILE_BUNDLE_NAME_CONFLICT")
    if len(exact) > 1:
        raise AppleWebWorkflowError("PROFILE_EXACT_MATCH_MULTIPLE")
    if len(exact) == 1:
        backend.click_text(app_name, exact=True)
        backend.wait_for_text("Download and Install")
        _verify_profile_detail(
            backend,
            app_name=app_name,
            bundle_id=bundle_id,
            team_id=team_id,
            identity=identity,
        )
        ledger.succeed(
            {
                "profile_state": "existing_exact",
                "certificate_sha256": certificate.sha256,
            }
        )
        return "existing_exact"
    if ledger.status != "planned":
        raise AppleWebWorkflowError("PROFILE_ATTEMPT_READ_ONLY_BLOCKED")
    backend.open_url(PROFILE_ADD_URL)
    backend.wait_for_text("Generate a Provisioning Profile")
    backend.click_text("App Store Connect", exact=True)
    backend.click_text("Continue", exact=True)
    backend.select_option("App ID", bundle_id)
    backend.click_text("Continue", exact=True)
    identity_label = re.sub(r"^Apple Distribution:\s*", "", identity.common_name)
    backend.click_text((identity.common_name, identity_label, "Apple Distribution"))
    backend.click_text("Continue", exact=True)
    backend.set_field("Provisioning Profile Name", app_name)
    ledger.begin()
    try:
        backend.click_text("Generate", exact=True)
    except Exception:
        ledger.mark_unknown()
        raise
    try:
        backend.wait_for_text("Download and Install", timeout=120)
    except Exception:
        ledger.mark_unknown()
        raise AppleWebWorkflowError("PROFILE_GENERATE_RESULT_UNKNOWN") from None
    _verify_profile_detail(
        backend,
        app_name=app_name,
        bundle_id=bundle_id,
        team_id=team_id,
        identity=identity,
    )
    ledger.succeed(
        {
            "profile_state": "generated",
            "certificate_sha256": certificate.sha256,
        }
    )
    return "generated"


def workflow_utm_13(
    backend: EdgeAXBackend, payload: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    app_name = _required(payload, "APP_NAME")
    bundle_id = _required(payload, "BUNDLE_ID")
    vm_user = _required(payload, "VM_USER")
    vm_name = _required(payload, "VM_NAME")
    run_id = _required(payload, "RUN_ID")
    team_id = _required(payload, "TEAM_ID")
    if not app_name.strip() or len(app_name) > 100:
        raise AppleWebWorkflowError("APP_NAME_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise AppleWebWorkflowError("BUNDLE_ID_INVALID")
    if not re.fullmatch(r"[A-Z0-9]{10}", team_id):
        raise AppleWebWorkflowError("TEAM_ID_INVALID")
    csr = _verify_csr_evidence(vm_user)
    certificate_ledger = StableUtm13Ledger(
        run_id=run_id,
        vm_name=vm_name,
        kind="certificate",
        details={
            "team_sha256": hashlib.sha256(team_id.encode()).hexdigest(),
            "csr_sha256": csr.sha256,
            "csr_public_key_sha256": csr.public_key_sha256,
            "bundle_sha256": hashlib.sha256(bundle_id.encode()).hexdigest(),
            "app_sha256": hashlib.sha256(app_name.encode()).hexdigest(),
        },
    )
    backend.open_url(CERTIFICATES_URL)
    backend.wait_for_text("Certificates")
    vm_password = _required(payload, "VM_PASSWORD")
    authorization_attempt_id = payload.get("AUTHORIZATION_ATTEMPT_ID", "current-run")
    if not isinstance(authorization_attempt_id, str):
        raise AppleWebWorkflowError("AUTHORIZATION_ATTEMPT_ID_INVALID")
    try:
        certificate, identity, certificate_state = _ensure_distribution_certificate(
            backend,
            vm_user=vm_user,
            vm_password=vm_password,
            team_id=team_id,
            csr=csr,
            ledger=certificate_ledger,
            authorization_attempt_id=authorization_attempt_id,
        )
    finally:
        vm_password = ""
    profile_ledger = StableUtm13Ledger(
        run_id=run_id,
        vm_name=vm_name,
        kind="profile",
        details={
            "team_sha256": hashlib.sha256(team_id.encode()).hexdigest(),
            "bundle_sha256": hashlib.sha256(bundle_id.encode()).hexdigest(),
            "app_sha256": hashlib.sha256(app_name.encode()).hexdigest(),
            "certificate_sha256": certificate.sha256,
            "certificate_identity_sha1": identity.sha1,
        },
    )
    profile_state = _generate_profile(
        backend,
        app_name=app_name,
        bundle_id=bundle_id,
        team_id=team_id,
        identity=identity,
        certificate=certificate,
        ledger=profile_ledger,
    )
    return [
        "EDGE_EXISTING_PID=verified",
        "LOCAL_BROWSER_SESSION=verified",
        "CSR_DISK=verified",
        "CERTIFICATES_PAGE=opened",
        f"APPLE_DISTRIBUTION_CERT={certificate_state}",
        "CODESIGN_IDENTITY=verified",
        "PROFILES_PAGE=opened",
        f"PROFILE_GENERATE_ATTEMPT_ID={profile_ledger.attempt_id}",
        "PROVISIONING_PROFILE=generated_or_existing_exact",
        "PROVISIONING_PROFILE_DOWNLOAD=ready",
        "UTM_13=verified",
    ], {
        "certificate_state": certificate_state,
        "profile_state": profile_state,
        "certificate_attempt_id": certificate_ledger.attempt_id,
        "profile_generate_attempt_id": profile_ledger.attempt_id,
        "certificate_sha256": certificate.sha256,
    }


WORKFLOWS = {
    "utm-10": workflow_utm_10,
    "utm-11": workflow_utm_11,
    "utm-12": workflow_utm_12,
    "utm-13": workflow_utm_13,
}

UTM_APPS_STAGE_MAP = {
    "utm-10": ("utm-10", None),
    "utm-11": ("utm-11", None),
    "utm-12-membership": ("utm-12", "membership"),
    "utm-12-apps": ("utm-12", "apps"),
    "utm-13": ("utm-13", None),
}


def _backend_for_workflow(
    workflow_name: str,
    *,
    reuse_existing: bool = False,
    expected_pid: int | None = None,
    starter: Any = start_utm10_edge,
    reuser: Any = reuse_utm10_edge,
    backend_factory: Any = EdgeAXBackend,
) -> tuple[EdgeAXBackend, int | None]:
    if workflow_name == "utm-10":
        pid = reuser() if reuse_existing else starter()
        return backend_factory(expected_pid=pid), pid
    if (
        not isinstance(expected_pid, int)
        or isinstance(expected_pid, bool)
        or expected_pid <= 0
    ):
        raise AppleWebWorkflowError("EXPECTED_EDGE_PID_INVALID")
    return backend_factory(expected_pid=expected_pid), None


def _canonical_stage(payload: Mapping[str, Any]) -> tuple[str, str, str | None]:
    if _required(payload, "WORKFLOW") != "utm-apps":
        raise AppleWebWorkflowError("WORKFLOW_INVALID")
    stage = _required(payload, "UTM_APPS_STAGE")
    try:
        internal_workflow, phase = UTM_APPS_STAGE_MAP[stage]
    except KeyError as error:
        raise AppleWebWorkflowError("UTM_APPS_STAGE_INVALID") from error
    return stage, internal_workflow, phase


def execute_utm_apps_stage(
    payload: Mapping[str, Any], expected_stage: str
) -> tuple[list[str], dict[str, Any]]:
    payload = dict(payload)
    stage, workflow_name, phase = _canonical_stage(payload)
    if stage != expected_stage:
        raise AppleWebWorkflowError("UTM_APPS_STAGE_SCRIPT_MISMATCH")
    if phase is not None:
        payload["UTM_12_PHASE"] = phase
    ledger = GuestAppsSessionLedger(payload)
    ledger.require_stage(stage)
    reuse_existing = payload.get("UTM_10_REUSE_EXISTING_EDGE") is True
    fill_account_only = payload.get("UTM_10_FILL_ACCOUNT_ONLY") is True
    remember_only = payload.get("UTM_10_CHECK_REMEMBER_ONLY") is True
    if remember_only:
        if stage != "utm-10" or not reuse_existing:
            raise AppleWebWorkflowError("REMEMBER_ONLY_REQUIRES_EXISTING_EDGE")
        markers, data = workflow_utm_10_remember_only_html(payload)
    elif fill_account_only:
        if stage != "utm-10" or not reuse_existing:
            raise AppleWebWorkflowError("FILL_ONLY_REQUIRES_EXISTING_EDGE")
        markers, data = workflow_utm_10_fill_only_html(payload)
    else:
        existing_session = ledger.session()
        if stage == "utm-10":
            if payload.get("EDGE_START_ALLOWED") is not True:
                raise AppleWebWorkflowError("EDGE_START_NOT_ALLOWED")
            reuse_existing = existing_session is not None
            expected_pid = existing_session[0] if existing_session else None
        else:
            if payload.get("EDGE_START_ALLOWED") is not False:
                raise AppleWebWorkflowError("EDGE_REUSE_REQUIRED")
            if existing_session is None:
                raise AppleWebWorkflowError("EDGE_SESSION_LEDGER_MISSING")
            expected_pid_value = payload.get("EXPECTED_EDGE_PID")
            expected_websocket = payload.get("EXPECTED_EDGE_WEBSOCKET")
            if (
                expected_pid_value != existing_session[0]
                or expected_websocket != existing_session[1]
            ):
                raise AppleWebWorkflowError("EDGE_SESSION_EXPECTATION_MISMATCH")
            expected_pid = existing_session[0]
        if existing_session is not None:
            live_session = edge_session_identity(existing_session[0])
            if live_session != existing_session:
                raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        if stage == "utm-12-apps" and payload.get("MEMBERSHIP_NOTION_ACK") is not True:
            raise AppleWebWorkflowError("MEMBERSHIP_NOTION_ACK_MISSING")
        backend, started_pid = _backend_for_workflow(
            workflow_name,
            reuse_existing=reuse_existing,
            expected_pid=expected_pid,
        )
        if started_pid is not None:
            payload["UTM_10_EDGE_STARTED_PID"] = started_pid
        elif stage == "utm-10" and existing_session is not None:
            payload["UTM_10_EDGE_STARTED_PID"] = existing_session[0]
            payload["UTM_10_REUSE_EXISTING_EDGE"] = True
        current_pid = backend.pid
        current_pid, current_websocket = edge_session_identity(current_pid)
        ledger.bind_session(current_pid, current_websocket)
        markers, data = WORKFLOWS[workflow_name](backend, payload)
        verified_pid, verified_websocket = edge_session_identity(current_pid)
        if (verified_pid, verified_websocket) != (current_pid, current_websocket):
            raise AppleWebWorkflowError("EDGE_PROCESS_CHANGED")
        ledger.complete_stage(stage)
        data = dict(data)
        data.update(
            {
                "edge_pid": current_pid,
                "edge_websocket": current_websocket,
                "completed_stages": list(ledger.completed_stages),
            }
        )
        if stage == "utm-13":
            markers = [*markers, "UTM_APPS=verified"]
    return list(markers), dict(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdin-json", action="store_true", required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise AppleWebWorkflowError("STDIN_JSON_NOT_OBJECT")
        stage = _required(payload, "UTM_APPS_STAGE")
        markers, data = execute_utm_apps_stage(payload, stage)
        print(json.dumps({"markers": markers, "data": data}, ensure_ascii=False))
        return 0
    except Exception as error:
        detail = _safe_error_detail(error)
        suffix = f" detail={detail}" if detail else ""
        print(
            f"APPLE_WEB_WORKFLOW=blocked reason={type(error).__name__}{suffix}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
