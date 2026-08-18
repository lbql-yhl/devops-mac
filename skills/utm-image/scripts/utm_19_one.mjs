#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { constants, createReadStream } from "node:fs";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const PLAYWRIGHT_CORE_URL = pathToFileURL(path.join(
  os.homedir(),
  "Downloads/Fire_One_en1.3/node_modules/playwright-core/index.mjs",
));
const APPS_URL = "https://appstoreconnect.apple.com/apps";
const DEFAULT_DISPLAY_NAME = '6.9" Display';
const CDP_PORT = 9222;
const RUNTIME_ROOT = path.join(os.homedir(), "Downloads", ".utm-image-runtime");
const MAX_STDIN_BYTES = 65_536;
const MAX_ARCHIVE_BYTES = 1_073_741_824;
const TIMEOUT = 30_000;

const SAFE_EXTRACT_PY = String.raw`
import os, stat, sys, unicodedata
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

archive = Path(sys.argv[1])
dest = Path(sys.argv[2])
if archive.is_symlink() or not archive.is_file():
    raise SystemExit("ARCHIVE_IDENTITY_INVALID")
if dest.exists() or dest.is_symlink():
    raise SystemExit("DESTINATION_NOT_NEW")

seen = set()
total = 0
members = 0
with ZipFile(archive) as bundle:
    bad_crc = bundle.testzip()
    if bad_crc is not None:
        raise SystemExit("ZIP_CRC_INVALID")
    for info in bundle.infolist():
        name = info.filename
        raw = name[:-1] if name.endswith("/") else name
        pieces = raw.split("/") if raw else []
        parsed = PurePosixPath(name)
        folded = unicodedata.normalize("NFC", name).casefold()
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        members += 1
        total += info.file_size
        ratio = info.file_size / max(info.compress_size, 1)
        if (not name or "\x00" in name or "\\" in name or parsed.is_absolute()
                or not pieces or any(part in ("", ".", "..") for part in pieces)
                or info.flag_bits & 0x1 or folded in seen
                or kind == stat.S_IFLNK
                or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                or members > 1000 or total > 1073741824 or ratio > 1000):
            raise SystemExit("ZIP_MEMBER_UNSAFE")
        seen.add(folded)
    dest.mkdir(mode=0o700)
    bundle.extractall(dest)

dest_real = dest.resolve(strict=True)
for item in dest.rglob("*"):
    current = item.lstat()
    if stat.S_ISLNK(current.st_mode) or not (stat.S_ISDIR(current.st_mode) or stat.S_ISREG(current.st_mode)):
        raise SystemExit("EXTRACTED_MEMBER_UNSAFE")
    resolved = item.resolve(strict=True)
    if resolved != dest_real and dest_real not in resolved.parents:
        raise SystemExit("EXTRACTED_PATH_ESCAPE")
print("SAFE_EXTRACT=verified")
`;

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function sha256File(file) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

function safeUrl(value) {
  const parsed = new URL(value);
  return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
}

function assertApplePage(value) {
  const parsed = new URL(value);
  const allowed = ["apple.com", "appstoreconnect.apple.com", "idmsa.apple.com"].some(
    (host) => parsed.hostname === host || parsed.hostname.endsWith(`.${host}`),
  );
  if (parsed.protocol !== "https:" || !allowed) throw new Error(`unexpected Apple page: ${safeUrl(value)}`);
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

async function readStdinJson() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > MAX_STDIN_BYTES) throw new Error("stdin JSON is too large");
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) throw new Error("one JSON object is required on stdin");
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("stdin must be a JSON object");
  const allowed = new Set([
    "runId", "vmName", "appStoreAppId", "appName", "displayName", "resumeExisting",
    "source", "expectedEdgePid", "expectedEdgeWebSocket",
  ]);
  for (const key of Object.keys(parsed)) if (!allowed.has(key)) throw new Error(`unsupported input field: ${key}`);
  if (typeof parsed.runId !== "string" || !/^[A-Za-z0-9_-]{8,100}$/.test(parsed.runId)) {
    throw new Error("runId is invalid");
  }
  if (typeof parsed.vmName !== "string" || !/^[a-z]{4}$/.test(parsed.vmName) || os.userInfo().username !== parsed.vmName) {
    throw new Error("vmName does not match the current guest user");
  }
  if (!/^\d+$/.test(String(parsed.appStoreAppId || ""))) {
    throw new Error("appStoreAppId must contain digits only");
  }
  if (typeof parsed.appName !== "string" || !parsed.appName.trim() || parsed.appName !== parsed.appName.trim()) {
    throw new Error("appName must be a non-empty string");
  }
  if (!Number.isSafeInteger(parsed.expectedEdgePid) || parsed.expectedEdgePid <= 0) {
    throw new Error("expectedEdgePid is invalid");
  }
  if (typeof parsed.expectedEdgeWebSocket !== "string" || !parsed.expectedEdgeWebSocket.startsWith("ws://127.0.0.1:9222/")) {
    throw new Error("expectedEdgeWebSocket is invalid");
  }
  if (parsed.resumeExisting !== undefined && parsed.resumeExisting !== true) {
    throw new Error("resumeExisting, when provided, must be true");
  }
  validateProvidedSource(parsed.source, parsed.appName);
  return parsed;
}

function validateProvidedSource(source, appName) {
  if (!source || typeof source !== "object" || Array.isArray(source)) throw new Error("source is invalid");
  const appNameSha256 = sha256(appName);
  if (source.appNameSha256 !== appNameSha256 || !/^[0-9a-f]{64}$/.test(String(source.sourceIdentitySha256 || ""))) {
    throw new Error("source application identity is invalid");
  }
  if (source.kind === "share_url" && source.sourceField === "美女截图 链接") {
    const allowed = new Set(["kind", "sourceField", "url", "sourceIdentitySha256", "appNameSha256"]);
    for (const key of Object.keys(source)) if (!allowed.has(key)) throw new Error(`unsupported source field: ${key}`);
    source.url = String(source.url).trim();
    if (sha256(source.url) !== source.sourceIdentitySha256) throw new Error("beauty source identity mismatch");
    return;
  }
  if (source.kind === "attachment" && source.sourceField === "研发截图") {
    const allowed = new Set(["kind", "sourceField", "path", "sha256", "size", "sourceIdentitySha256", "appNameSha256"]);
    for (const key of Object.keys(source)) if (!allowed.has(key)) throw new Error(`unsupported source field: ${key}`);
    if (
      typeof source.path !== "string" || !source.path.startsWith("/Volumes/My Shared Files/共享文件/utm-image/") ||
      source.path.includes("\0") || !/^[0-9a-f]{64}$/.test(String(source.sha256 || "")) ||
      !Number.isSafeInteger(source.size) || source.size <= 0 || source.size > MAX_ARCHIVE_BYTES
    ) throw new Error("development attachment source is invalid");
    return;
  }
  throw new Error("source kind does not match an approved Feishu field");
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
    child.stdin.end(input || undefined);
  });
}

function sourceFieldMarker(source) {
  if (source?.sourceField === "美女截图 链接" && source?.kind === "share_url") return "beauty_link";
  if (source?.sourceField === "研发截图" && source?.kind === "attachment") return "development_attachment";
  throw new Error("Feishu screenshot source marker is invalid");
}

async function browserVersion() {
  try {
    const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, {
      signal: AbortSignal.timeout(7_000),
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

async function verifyExistingEdgeIdentity(expectedPid, expectedWebSocket) {
  const [processResult, listenerResult, info] = await Promise.all([
    command("/usr/bin/pgrep", ["-x", "Microsoft Edge"]),
    command("/usr/sbin/lsof", ["-nP", "-iTCP:9222", "-sTCP:LISTEN", "-t"]),
    browserVersion(),
  ]);
  const pids = [...new Set(processResult.stdout.split(/\s+/).filter((value) => /^\d+$/.test(value)))];
  const listenerPids = [...new Set(listenerResult.stdout.split(/\s+/).filter((value) => /^\d+$/.test(value)))];
  if (processResult.code !== 0 || pids.length !== 1 || Number(pids[0]) !== expectedPid) {
    throw new Error("existing Edge process identity changed");
  }
  if (listenerResult.code !== 0 || listenerPids.length !== 1 || Number(listenerPids[0]) !== expectedPid) {
    throw new Error("existing Edge listener identity changed");
  }
  const liveSessionId = sessionIdFromVersion(info);
  if (!info || !liveSessionId || info.webSocketDebuggerUrl !== expectedWebSocket) {
    throw new Error("existing Edge WebSocket identity changed");
  }
  return { info, liveSessionId };
}

async function connectExistingEdge(expectedPid, expectedWebSocket) {
  const before = await verifyExistingEdgeIdentity(expectedPid, expectedWebSocket);
  const { chromium } = await import(PLAYWRIGHT_CORE_URL.href);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`);
  const after = await verifyExistingEdgeIdentity(expectedPid, expectedWebSocket);
  if (after.liveSessionId !== before.liveSessionId) throw new Error("Edge session changed while connecting");
  const context = browser.contexts()[0];
  if (!context) throw new Error("existing Edge process has no persistent browser context");
  return { browser, context, sessionId: before.liveSessionId, edgePid: expectedPid };
}

async function exactlyOne(locator, label) {
  const count = await locator.count();
  if (count !== 1) throw new Error(`${label} must match exactly once; found ${count}`);
  const item = locator.first();
  if (!await item.isVisible()) throw new Error(`${label} is not visible`);
  return item;
}

async function firstVisibleControl(locators, label) {
  for (const locator of locators) {
    const count = await locator.count();
    for (let index = 0; index < count; index += 1) {
      const item = locator.nth(index);
      if (await item.isVisible()) return item;
    }
  }
  throw new Error(`${label} was not found`);
}

async function settleAfterGuiAction(page) {
  await page.waitForTimeout(3_000);
  const state = await page.evaluate(() => ({
    url: window.location.href,
    readyState: document.readyState,
    visibilityState: document.visibilityState,
  }));
  if (
    !state || typeof state.url !== "string" || !state.url ||
    !["interactive", "complete"].includes(state.readyState) ||
    !["visible", "hidden"].includes(state.visibilityState)
  ) {
    throw new Error("fresh browser state readback failed after GUI action");
  }
  return state;
}

async function downloadPackage(context, shareUrl, runDir) {
  const page = await context.newPage();
  await page.goto(shareUrl, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await settleAfterGuiAction(page);
  let button;
  try {
    button = await firstVisibleControl([
      page.getByRole("button", { name: /下载|download/i }),
      page.getByRole("link", { name: /下载|download/i }),
      page.locator("button, a, [role='button'], [role='link'], [class*='download' i], [aria-label*='download' i], [title*='download' i]").filter({ hasText: /下载|download/i }),
      page.getByText(/下载|download/i),
    ], "download button");
  } catch (error) {
    const title = String(await page.title().catch(() => "")).replace(/\s+/g, " ").trim().slice(0, 120);
    const text = String(await page.locator("body").innerText().catch(() => "")).replace(/\s+/g, " ").trim().slice(0, 300);
    throw new Error(`${error.message}; title=${title || "<empty>"}; visible text=${text || "<empty>"}`);
  }
  if (!await button.isEnabled()) throw new Error("download button is disabled");
  const download = await Promise.all([
    page.waitForEvent("download", { timeout: 60_000 }),
    button.click({ timeout: TIMEOUT }),
  ]).then(([item]) => item);
  await settleAfterGuiAction(page);
  const failure = await download.failure();
  if (failure) throw new Error("browser download failed");
  const suggested = path.basename(download.suggestedFilename() || "screenshots.zip");
  const safeName = suggested.replace(/[^\p{L}\p{N}._-]+/gu, "_") || "screenshots.zip";
  const archive = path.join(runDir, "download", safeName);
  await mkdir(path.dirname(archive), { recursive: true, mode: 0o700 });
  await download.saveAs(archive);
  await chmod(archive, 0o600);
  const metadata = await stat(archive);
  if (!metadata.isFile() || metadata.size < 4 || metadata.size > MAX_ARCHIVE_BYTES) {
    throw new Error("downloaded package has an invalid size");
  }
  const handle = await open(archive, "r");
  const magic = Buffer.alloc(8);
  try {
    await handle.read(magic, 0, magic.length, 0);
  } finally {
    await handle.close();
  }
  const isZip = magic[0] === 0x50 && magic[1] === 0x4b && [0x03, 0x05, 0x07].includes(magic[2]);
  const isJpeg = magic[0] === 0xff && magic[1] === 0xd8 && magic[2] === 0xff;
  const isPng = magic.equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  if (!isZip && !isJpeg && !isPng) throw new Error("downloaded package is not ZIP, JPEG, or PNG");
  return { page, archive, isZip, digest: await sha256File(archive), size: metadata.size };
}

async function preparedAttachmentPackage(source, runDir) {
  if (source.kind !== "attachment" || source.sourceField !== "研发截图" || typeof source.path !== "string") {
    throw new Error("Feishu development screenshot attachment is not verified");
  }
  const sharedArchive = await realpath(source.path);
  const sharedRoot = await realpath("/Volumes/My Shared Files/共享文件/utm-image");
  if (sharedArchive === sharedRoot || !sharedArchive.startsWith(`${sharedRoot}${path.sep}`)) {
    throw new Error("Feishu development screenshot attachment escaped the shared staging root");
  }
  const sharedInfo = await lstat(sharedArchive);
  if (!sharedInfo.isFile() || sharedInfo.isSymbolicLink() || sharedInfo.size < 4 || sharedInfo.size > MAX_ARCHIVE_BYTES) {
    throw new Error("Feishu development screenshot attachment is not a secure regular file");
  }
  const suffix = path.extname(sharedArchive).toLowerCase();
  if (![".zip", ".jpg", ".jpeg", ".png"].includes(suffix)) throw new Error("development attachment extension is invalid");
  const sourceDir = path.join(runDir, "source");
  await mkdir(sourceDir, { recursive: true, mode: 0o700 });
  await chmod(sourceDir, 0o700);
  const archive = path.join(sourceDir, `development-source${suffix}`);
  await copyFile(sharedArchive, archive, constants.COPYFILE_EXCL);
  await chmod(archive, 0o600);
  const info = await lstat(archive);
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o777) !== 0o600 || info.size !== sharedInfo.size) {
    throw new Error("guest development screenshot copy is not a secure regular file");
  }
  const handle = await open(archive, "r");
  const magic = Buffer.alloc(8);
  try {
    await handle.read(magic, 0, magic.length, 0);
  } finally {
    await handle.close();
  }
  const isZip = magic[0] === 0x50 && magic[1] === 0x4b && [0x03, 0x05, 0x07].includes(magic[2]);
  const isJpeg = magic[0] === 0xff && magic[1] === 0xd8 && magic[2] === 0xff;
  const isPng = magic.equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  if (!isZip && !isJpeg && !isPng) throw new Error("研发截图 attachment is not ZIP, JPEG, or PNG");
  const digest = await sha256File(archive);
  if (digest !== source.sha256 || info.size !== source.size) {
    throw new Error("Feishu development screenshot attachment readback mismatch");
  }
  return { archive, isZip, digest, size: info.size, runDir };
}

async function safeExtract(archive, runDir) {
  const destination = path.join(runDir, "extracted");
  const result = await command("/usr/bin/python3", ["-c", SAFE_EXTRACT_PY, archive, destination]);
  if (result.code !== 0 || !result.stdout.includes("SAFE_EXTRACT=verified")) {
    throw new Error(`safe ZIP extraction failed: ${(result.stderr || result.stdout).trim() || `exit ${result.code}`}`);
  }
  return destination;
}

async function walkFiles(root) {
  const output = [];
  async function visit(directory) {
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name);
      const info = await lstat(absolute);
      if (info.isSymbolicLink()) throw new Error("symbolic link found in extracted package");
      if (info.isDirectory()) await visit(absolute);
      else if (info.isFile()) output.push(absolute);
      else throw new Error("special file found in extracted package");
    }
  }
  await visit(root);
  return output;
}

async function validateImages(root) {
  const rootReal = await realpath(root);
  const files = await walkFiles(root);
  const images = [];
  const identities = new Set();
  for (const file of files) {
    if (path.basename(file).startsWith("._")) continue;
    const extension = path.extname(file).toLowerCase();
    if (![".jpg", ".jpeg", ".png"].includes(extension)) continue;
    const info = await lstat(file);
    const resolved = await realpath(file);
    if (!info.isFile() || info.isSymbolicLink() || (resolved !== rootReal && !resolved.startsWith(`${rootReal}${path.sep}`))) {
      throw new Error("image path escaped the extraction directory");
    }
    if (identities.has(resolved)) throw new Error("duplicate image identity found");
    identities.add(resolved);
    const bytes = await readFile(file);
    const jpeg = bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
    const png = bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    if (!bytes.length || (!jpeg && !png)) throw new Error(`image magic mismatch: ${path.basename(file)}`);
    const probe = await command("/usr/bin/sips", ["-g", "pixelWidth", "-g", "pixelHeight", file]);
    if (probe.code !== 0 || !/pixelWidth:\s*\d+/.test(probe.stdout) || !/pixelHeight:\s*\d+/.test(probe.stdout)) {
      throw new Error(`image dimensions are unreadable: ${path.basename(file)}`);
    }
    const width = Number(probe.stdout.match(/pixelWidth:\s*(\d+)/)?.[1]);
    const height = Number(probe.stdout.match(/pixelHeight:\s*(\d+)/)?.[1]);
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height)) {
      throw new Error(`image dimensions are invalid: ${path.basename(file)}`);
    }
    images.push({ file, name: path.relative(root, file), digest: sha256(bytes), size: bytes.length, width, height });
  }
  images.sort((left, right) => left.name.localeCompare(right.name, "en", { numeric: true }));
  if (images.length < 1 || images.length > 10) throw new Error(`expected 1-10 upload images; found ${images.length}`);
  return images;
}

async function normalizeImagesForDisplay(images, sessionDir, targetDimensions = { width: 1284, height: 2778 }) {
  const acceptedPortrait = new Set([`${targetDimensions.width}x${targetDimensions.height}`]);
  const acceptedLandscape = new Set([`${targetDimensions.height}x${targetDimensions.width}`]);
  if (images.every((image) => acceptedPortrait.has(`${image.width}x${image.height}`) || acceptedLandscape.has(`${image.width}x${image.height}`))) {
    return images;
  }
  const sourceOrientations = new Set(images.map((image) => image.width < image.height ? "portrait" : image.width > image.height ? "landscape" : "square"));
  if (sourceOrientations.size !== 1 || sourceOrientations.has("square")) {
    throw new Error("screenshot dimensions require normalization but orientation is inconsistent");
  }
  const portrait = sourceOrientations.has("portrait");
  const targetWidth = portrait ? targetDimensions.width : targetDimensions.height;
  const targetHeight = portrait ? targetDimensions.height : targetDimensions.width;
  const normalizedDir = path.join(sessionDir, "normalized", randomUUID());
  await mkdir(normalizedDir, { recursive: true, mode: 0o700 });
  await chmod(normalizedDir, 0o700);
  const normalized = [];
  for (let index = 0; index < images.length; index += 1) {
    const source = images[index];
    const extension = path.extname(source.file).toLowerCase() === ".png" ? ".png" : ".jpg";
    const target = path.join(normalizedDir, `${String(index + 1).padStart(2, "0")}${extension}`);
    await copyFile(source.file, target, constants.COPYFILE_EXCL);
    const resized = await command("/usr/bin/sips", ["-z", String(targetHeight), String(targetWidth), target]);
    if (resized.code !== 0) throw new Error("screenshot normalization failed");
    const checked = await validateImages(normalizedDir);
    if (checked.length !== index + 1) throw new Error("normalized screenshot set readback mismatch");
    normalized.splice(0, normalized.length, ...checked);
  }
  if (!normalized.every((image) => image.width === targetWidth && image.height === targetHeight)) {
    throw new Error("normalized screenshot dimensions are wrong");
  }
  return normalized;
}

async function writePackageManifest(runDir, source, downloaded, appName) {
  const runRoot = await realpath(runDir);
  const archive = await realpath(downloaded.archive);
  if (archive === runRoot || !archive.startsWith(`${runRoot}${path.sep}`)) {
    throw new Error("prepared package escaped the guest run directory");
  }
  const relativeArchive = path.relative(runRoot, archive);
  await writeSecureJson(path.join(runDir, "source.json"), {
    status: "verified",
    sourceField: source.sourceField,
    kind: source.kind,
    appNameSha256: sha256(appName),
    sourceIdentitySha256: source.sourceIdentitySha256,
    archiveRelative: relativeArchive,
    archiveDigest: downloaded.digest,
    size: downloaded.size,
    isZip: downloaded.isZip,
  });
}

async function reusablePackage(sessionDir, appName, sourceIdentitySha256) {
  const runsDir = path.join(sessionDir, "runs");
  const entries = await readdir(runsDir, { withFileTypes: true }).catch(() => []);
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const runDir = path.join(runsDir, entry.name);
    try {
      const manifestPath = path.join(runDir, "source.json");
      const manifestInfo = await lstat(manifestPath);
      if (!manifestInfo.isFile() || manifestInfo.isSymbolicLink() || (manifestInfo.mode & 0o777) !== 0o600) continue;
      const source = await readJson(manifestPath);
      if (
        source?.status !== "verified" || source?.appNameSha256 !== sha256(appName) ||
        source?.sourceIdentitySha256 !== sourceIdentitySha256
      ) continue;
      const marker = sourceFieldMarker(source);
      if (typeof source.archiveRelative !== "string" || path.isAbsolute(source.archiveRelative)) continue;
      const runRoot = await realpath(runDir);
      const archive = await realpath(path.join(runDir, source.archiveRelative));
      if (archive === runRoot || !archive.startsWith(`${runRoot}${path.sep}`)) continue;
      const archiveInfo = await lstat(archive);
      if (!archiveInfo.isFile() || archiveInfo.isSymbolicLink() || archiveInfo.size !== source.size) continue;
      const archiveDigest = await sha256File(archive);
      if (archiveDigest !== source.archiveDigest) continue;
      const imageRoot = source.isZip ? path.join(runDir, "extracted") : path.dirname(archive);
      const images = await validateImages(imageRoot);
      const runInfo = await stat(runDir);
      candidates.push({
        runDir,
        archive,
        archiveDigest,
        isZip: source.isZip,
        images,
        imageDigest: imageSetDigest(images),
        sourceFieldMarker: marker,
        modifiedMs: runInfo.mtimeMs,
      });
    } catch {}
  }
  if (!candidates.length) throw new Error("no previously verified screenshot package is available to resume");
  const identities = new Set(candidates.map((item) => `${item.archiveDigest}:${item.imageDigest}`));
  if (identities.size !== 1) throw new Error("previous screenshot packages differ; refusing to choose one");
  candidates.sort((left, right) => right.modifiedMs - left.modifiedMs);
  return candidates[0];
}

function appIdsFromPages(pages) {
  const ids = new Set();
  for (const page of pages) {
    try {
      const parsed = new URL(page.url());
      if (parsed.hostname !== "appstoreconnect.apple.com") continue;
      const match = parsed.pathname.match(/^\/apps\/(\d+)(?:\/|$)/);
      if (match) ids.add(match[1]);
    } catch {}
  }
  return [...ids];
}

function exactCurrentAppPage(context, requestedId) {
  const candidates = context.pages().filter((page) => {
    try {
      const parsed = new URL(page.url());
      return parsed.hostname === "appstoreconnect.apple.com"
        && new RegExp(`^/apps/${requestedId}(?:/|$)`).test(parsed.pathname);
    } catch {
      return false;
    }
  });
  return candidates.length === 1 ? candidates[0] : null;
}

async function chooseAppFromAppsPage(page, requested) {
  await page.goto(APPS_URL, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await settleAfterGuiAction(page);
  assertApplePage(page.url());
  if (new URL(page.url()).hostname !== "appstoreconnect.apple.com") {
    throw new Error("App Store Connect authentication is required in the existing Edge session");
  }
  let appId = requested.id;
  if (appId) {
    await page.goto(`https://appstoreconnect.apple.com/apps/${appId}`, {
      waitUntil: "domcontentloaded",
      timeout: TIMEOUT,
    });
    await settleAfterGuiAction(page);
  } else if (requested.name) {
    const named = page.getByText(requested.name, { exact: true });
    await named.first().waitFor({ state: "visible", timeout: TIMEOUT });
    const visible = [];
    for (let index = 0; index < await named.count(); index += 1) {
      const item = named.nth(index);
      if (await item.isVisible()) visible.push(item);
    }
    if (visible.length !== 1) {
      throw new Error(`App Store app name must have one visible exact match; found ${visible.length}`);
    }
    await visible[0].click({ timeout: TIMEOUT });
    await settleAfterGuiAction(page);
    await page.waitForURL(/\/apps\/\d+(?:\/|$)/, { timeout: TIMEOUT });
    const match = new URL(page.url()).pathname.match(/^\/apps\/(\d+)(?:\/|$)/);
    if (!match) throw new Error("clicked app page does not contain a numeric App Store ID");
    appId = match[1];
  } else {
    const links = page.locator('a[href*="/apps/"]');
    const found = new Map();
    for (let index = 0; index < await links.count(); index += 1) {
      const item = links.nth(index);
      if (!await item.isVisible()) continue;
      const href = await item.getAttribute("href");
      const match = href?.match(/\/apps\/(\d+)(?:\/|$)/);
      if (match && !found.has(match[1])) found.set(match[1], item);
    }
    if (found.size !== 1) throw new Error(`cannot uniquely infer app from Apps page; found ${found.size}`);
    [appId] = found.keys();
    await found.get(appId).click({ timeout: TIMEOUT });
    await settleAfterGuiAction(page);
  }
  await page.waitForURL(new RegExp(`/apps/${appId}(?:/|$)`), { timeout: TIMEOUT });
  assertApplePage(page.url());
  if (!new URL(page.url()).pathname.includes(`/apps/${appId}`)) throw new Error("App Store app identity mismatch");
  return appId;
}

async function currentMediaManagerPage(context, requested, displayName, { allowNone = false } = {}) {
  const candidates = [];
  for (const page of context.pages()) {
    try {
      const parsed = new URL(page.url());
      if (parsed.hostname !== "appstoreconnect.apple.com") continue;
      const match = parsed.pathname.match(/^\/apps\/(\d+)(?:\/|$)/);
      if (!match || (requested.id && requested.id !== match[1])) continue;
      const mediaTitle = page.getByText("Media Manager", { exact: true });
      const display = page.getByText(displayName, { exact: true });
      if (!await mediaTitle.first().isVisible().catch(() => false)) continue;
      if (!await display.first().isVisible().catch(() => false)) continue;
      if (requested.name) {
        const names = page.getByText(requested.name, { exact: true });
        let visibleName = false;
        for (let index = 0; index < await names.count(); index += 1) {
          if (await names.nth(index).isVisible()) visibleName = true;
        }
        if (!visibleName) continue;
      }
      const visibility = await page.evaluate(() => document.visibilityState);
      candidates.push({ page, appId: match[1], active: visibility === "visible" });
    } catch {}
  }
  const active = candidates.filter((item) => item.active);
  if (active.length === 1) return active[0];
  if (candidates.length === 1) return candidates[0];
  if (allowNone && candidates.length === 0) return null;
  throw new Error(`current Media Manager page must be unique; found ${candidates.length}`);
}

async function openAppVersionPage(page, appId, displayName) {
  const parsed = new URL(page.url());
  if (parsed.hostname !== "appstoreconnect.apple.com" || !new RegExp(`^/apps/${appId}(?:/|$)`).test(parsed.pathname)) {
    throw new Error("App Store app identity mismatch before opening the version page");
  }
  const display = page.getByText(displayName, { exact: true });
  if (await display.count() === 1 && await display.first().isVisible()) return;
  const media = page.getByRole("link", { name: /View All Sizes in Media Manager/i })
    .or(page.getByRole("button", { name: /View All Sizes in Media Manager/i }));
  for (let index = 0; index < await media.count(); index += 1) {
    if (await media.nth(index).isVisible()) return;
  }
  await page.goto(
    `https://appstoreconnect.apple.com/apps/${appId}/distribution/ios/version/inflight`,
    { waitUntil: "domcontentloaded", timeout: TIMEOUT },
  );
  await settleAfterGuiAction(page);
  assertApplePage(page.url());
  const current = new URL(page.url());
  if (current.hostname !== "appstoreconnect.apple.com" || !new RegExp(`^/apps/${appId}(?:/|$)`).test(current.pathname)) {
    throw new Error("App Store app identity mismatch after opening the version page");
  }
}

async function openMediaManager(page, displayName) {
  for (let attempt = 0; attempt < 12; attempt += 1) {
    const existing = page.getByText(displayName, { exact: true });
    if (await existing.count() === 1 && await existing.first().isVisible()) return;
    const media = page.getByRole("link", { name: /View All Sizes in Media Manager/i })
      .or(page.getByRole("button", { name: /View All Sizes in Media Manager/i }));
    const visible = [];
    for (let index = 0; index < await media.count(); index += 1) {
      const item = media.nth(index);
      if (await item.isVisible()) visible.push(item);
    }
    if (visible.length === 1) {
      await visible[0].click({ timeout: TIMEOUT });
      await settleAfterGuiAction(page);
      break;
    }
    await page.evaluate(() => window.scrollBy(0, Math.max(500, window.innerHeight * 0.8)));
    await settleAfterGuiAction(page);
  }
  const label = page.getByText(displayName, { exact: true });
  await label.waitFor({ state: "visible", timeout: TIMEOUT });
}

async function displayPanel(page, displayName) {
  const label = await exactlyOne(page.getByText(displayName, { exact: true }), `${displayName} label`);
  const displayButton = label.locator("xpath=ancestor::button[1]");
  if (await displayButton.count() !== 1) throw new Error(`${displayName} selector is not unique`);
  if (await displayButton.getAttribute("aria-expanded") !== "true") {
    await displayButton.click({ timeout: TIMEOUT });
    await settleAfterGuiAction(page);
  }
  const regionId = await displayButton.getAttribute("aria-controls");
  if (!regionId) throw new Error(`${displayName} panel identity is missing`);
  const panel = page.locator(`[id=${JSON.stringify(regionId)}]`);
  if (await panel.count() !== 1 || !await panel.isVisible()) throw new Error(`${displayName} upload panel is not unique`);
  const panelInputs = panel.locator('input[type="file"]');
  if (await panelInputs.count() !== 1) throw new Error(`${displayName} screenshot file input is not unique`);
  const input = panelInputs.first();
  const text = await panel.innerText();
  const matches = [...text.matchAll(/(\d+)\s+of\s+10\s+Screenshots/gi)];
  const values = [...new Set(matches.map((match) => match[1]))];
  if (values.length !== 1) throw new Error(`${displayName} screenshot count is not unique`);
  const current = Number(values[0]);
  if (!Number.isInteger(current) || current < 0 || current > 10) throw new Error("invalid screenshot count");
  if (!await input.isEnabled()) throw new Error(`${displayName} file input is disabled`);
  return { panel, input, current };
}

async function acceptedDimensionsFromPanel(panel) {
  const text = await panel.innerText();
  const pairs = [...text.matchAll(/(\d{3,4})\s*[\u00d7x]\s*(\d{3,4})\s*px/gi)].map((match) => ({
    width: Number(match[1]),
    height: Number(match[2]),
  }));
  const portrait = pairs.filter((item) => item.width < item.height);
  if (!portrait.length) throw new Error("Media Manager accepted screenshot dimensions are missing");
  portrait.sort((left, right) => (right.width * right.height) - (left.width * left.height));
  return portrait[0];
}

function imageSetDigest(images) {
  return sha256(JSON.stringify(images.map(({ name, digest, size }) => ({ name, digest, size }))));
}

async function waitForUploadedCount(page, displayName, expected) {
  const delays = [5_000, 10_000, 20_000, 40_000];
  for (const delay of delays) {
    await page.waitForTimeout(delay);
    const state = await displayPanel(page, displayName);
    const text = await state.panel.innerText();
    if (/failed|error|invalid|not accepted|couldn.?t/i.test(text)) throw new Error("App Store Connect reported an upload error");
    if (state.current === expected) return state;
  }
  throw new Error(`upload result is ambiguous; expected ${expected} of 10 screenshots`);
}

async function readStableScreenshotCount(page, displayName) {
  const counts = [];
  for (const delay of [0, 5_000, 10_000]) {
    if (delay) await page.waitForTimeout(delay);
    counts.push((await displayPanel(page, displayName)).current);
  }
  return counts.every((count) => count === counts[0]) ? counts[0] : null;
}

async function screenshotDimensionsError(page) {
  const messages = page.getByText(/dimensions of one or more screenshots are wrong/i);
  for (let index = 0; index < await messages.count(); index += 1) {
    if (await messages.nth(index).isVisible()) return true;
  }
  return false;
}

async function uploadImages(page, sessionId, appId, displayName, images, ledgerFile, archiveDigest) {
  await openMediaManager(page, displayName);
  let state = await displayPanel(page, displayName);
  const targetDimensions = await acceptedDimensionsFromPanel(state.panel);
  let setDigest = imageSetDigest(images);
  const existing = await readJson(ledgerFile);
  let sameAttempt = existing && existing.appId === appId && existing.displayName === displayName &&
    existing.imageSetDigest === setDigest && existing.archiveDigest === archiveDigest && existing.count === images.length;
  if (existing?.state === "unknown" && existing.appId === appId && existing.displayName === displayName && existing.archiveDigest === archiveDigest) {
    const normalizedImages = await normalizeImagesForDisplay(images, path.dirname(ledgerFile), targetDimensions);
    const stableCount = await readStableScreenshotCount(page, displayName);
    if (stableCount === images.length && !(await screenshotDimensionsError(page))) {
      await writeSecureJson(ledgerFile, { ...existing, state: "verified", verifiedAt: new Date().toISOString() });
      return "already_complete";
    }
    if (stableCount === 0 && await screenshotDimensionsError(page)) {
      images = normalizedImages;
      setDigest = imageSetDigest(normalizedImages);
      sameAttempt = false;
      await writeSecureJson(ledgerFile, { ...existing, state: "rejected_dimensions", rejectedAt: new Date().toISOString() });
      existing.state = "rejected_dimensions";
    }
  }
  if (existing?.state === "rejected_dimensions" && existing.appId === appId && existing.displayName === displayName && existing.archiveDigest === archiveDigest) {
    const stableCount = await readStableScreenshotCount(page, displayName);
    if (stableCount !== 0) throw new Error("rejected screenshot dimensions have a non-empty upload result");
    images = await normalizeImagesForDisplay(images, path.dirname(ledgerFile), targetDimensions);
    setDigest = imageSetDigest(images);
    sameAttempt = false;
  }
  if (sameAttempt && ["clicking", "submitted", "unknown", "verified"].includes(existing.state)) {
    const stableCount = await readStableScreenshotCount(page, displayName);
    if (await screenshotDimensionsError(page)) {
      if (stableCount !== 0) throw new Error("screenshot dimensions are wrong with a non-empty upload result");
      await writeSecureJson(ledgerFile, {
        ...existing,
        state: "rejected_dimensions",
        rejectedAt: new Date().toISOString(),
      });
      images = await normalizeImagesForDisplay(images, path.dirname(ledgerFile), targetDimensions);
      setDigest = imageSetDigest(images);
      sameAttempt = false;
    }
    if (sameAttempt && stableCount === images.length) {
      await writeSecureJson(ledgerFile, {
        ...existing,
        sessionId,
        state: "verified",
        verifiedAt: new Date().toISOString(),
      });
      return "already_complete";
    }
    if (sameAttempt) throw new Error("previous upload attempt is unresolved; refusing to upload again");
  }
  if (existing && !sameAttempt && !["planned", "rejected_dimensions"].includes(existing.state)) {
    throw new Error("a different screenshot upload attempt already exists for this Edge session");
  }
  if (state.current !== 0) throw new Error(`6.9-inch display already contains ${state.current} screenshot(s)`);
  if (images.length > 10 - state.current) throw new Error("screenshot package exceeds remaining capacity");
  const attempt = {
    attemptId: sameAttempt ? existing.attemptId : randomUUID(),
    state: "planned",
    sessionId,
    appId,
    displayName,
    count: images.length,
    archiveDigest,
    imageSetDigest: setDigest,
    files: images.map(({ name, digest, size }) => ({ name, digest, size })),
    preparedAt: new Date().toISOString(),
  };
  await writeSecureJson(ledgerFile, attempt);
  await writeSecureJson(ledgerFile, { ...attempt, state: "clicking", clickingAt: new Date().toISOString() });
  try {
    await state.input.setInputFiles(images.map(({ file }) => file), { timeout: TIMEOUT });
    await settleAfterGuiAction(page);
    await writeSecureJson(ledgerFile, { ...attempt, state: "submitted", submittedAt: new Date().toISOString() });
    state = await waitForUploadedCount(page, displayName, images.length);
  } catch (error) {
    await writeSecureJson(ledgerFile, {
      ...attempt,
      state: "unknown",
      failedAt: new Date().toISOString(),
      nonSensitiveError: String(error.message || error).slice(0, 500),
    });
    throw error;
  }
  await writeSecureJson(ledgerFile, { ...attempt, state: "verified", verifiedAt: new Date().toISOString() });
  return "uploaded";
}

async function selfTest() {
  if (sourceFieldMarker({ sourceField: "美女截图 链接", kind: "share_url" }) !== "beauty_link") {
    throw new Error("beauty source marker self-test failed");
  }
  if (sourceFieldMarker({ sourceField: "研发截图", kind: "attachment" }) !== "development_attachment") {
    throw new Error("development source marker self-test failed");
  }
  const temporary = await mkdtemp(path.join(os.tmpdir(), "utm-image-self-test-"));
  try {
    const validArchive = path.join(temporary, "valid.zip");
    const unsafeArchive = path.join(temporary, "unsafe.zip");
    const fixture = String.raw`
from zipfile import ZipFile
import sys
with ZipFile(sys.argv[1], "w") as z: z.writestr("screens/one.jpg", b"test")
with ZipFile(sys.argv[2], "w") as z: z.writestr("../escape.jpg", b"test")
`;
    const created = await command("/usr/bin/python3", ["-c", fixture, validArchive, unsafeArchive]);
    if (created.code !== 0) throw new Error("ZIP fixture self-test failed");
    const validRun = path.join(temporary, "valid-run");
    const unsafeRun = path.join(temporary, "unsafe-run");
    await mkdir(validRun, { mode: 0o700 });
    await mkdir(unsafeRun, { mode: 0o700 });
    await safeExtract(validArchive, validRun);
    let unsafeRejected = false;
    try { await safeExtract(unsafeArchive, unsafeRun); } catch { unsafeRejected = true; }
    if (!unsafeRejected) throw new Error("unsafe ZIP self-test failed");
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
  process.stdout.write("UTM_19_SELF_TEST=verified\n");
}

async function main() {
  if (process.argv.includes("--self-test")) {
    await selfTest();
    return;
  }
  if (process.argv.length !== 2) throw new Error("utm-image accepts input only through stdin JSON");
  const input = await readStdinJson();
  const requested = {
    id: String(input.appStoreAppId),
    name: input.appName,
  };
  const displayName = typeof input.displayName === "string" && input.displayName.trim()
    ? input.displayName.trim() : DEFAULT_DISPLAY_NAME;
  const connected = await connectExistingEdge(input.expectedEdgePid, input.expectedEdgeWebSocket);
  const pageIds = appIdsFromPages(connected.context.pages());
  if (pageIds.length > 1 && !pageIds.includes(requested.id)) throw new Error("current App Store app identity is ambiguous");

  const sessionDir = path.join(RUNTIME_ROOT, input.runId, connected.sessionId);
  let downloaded;
  let images;
  let appPage;
  let appId;
  let appPageMode;
  let sourceMarker;
  const current = await currentMediaManagerPage(
    connected.context,
    requested,
    displayName,
    { allowNone: true },
  );
  let reusable = null;
  if (current) reusable = await reusablePackage(
    sessionDir,
    requested.name,
    input.source.sourceIdentitySha256,
  ).catch(() => null);
  if (input.resumeExisting === true && (!current || !reusable)) {
    throw new Error("resumeExisting requires one current Media Manager page and one verified existing package");
  }
  if (current && reusable) {
    downloaded = { digest: reusable.archiveDigest, isZip: reusable.isZip };
    images = reusable.images;
    sourceMarker = reusable.sourceFieldMarker;
    appPage = current.page;
    appId = current.appId;
    appPageMode = "reused_current_media_manager";
  } else {
    const runDir = path.join(sessionDir, "runs", randomUUID());
    await mkdir(runDir, { recursive: true, mode: 0o700 });
    await chmod(runDir, 0o700);
    const source = input.source;
    sourceMarker = sourceFieldMarker(source);
    downloaded = source.kind === "share_url"
      ? await downloadPackage(connected.context, source.url, runDir)
      : await preparedAttachmentPackage(source, runDir);
    const imageRoot = downloaded.isZip ? await safeExtract(downloaded.archive, runDir) : path.dirname(downloaded.archive);
    images = await validateImages(imageRoot);
    await writePackageManifest(runDir, source, downloaded, requested.name);
    const currentAfterDownload = await currentMediaManagerPage(
      connected.context,
      requested,
      displayName,
      { allowNone: true },
    );
    if (currentAfterDownload) {
      appPage = currentAfterDownload.page;
      appId = currentAfterDownload.appId;
      appPageMode = "reused_current_media_manager";
    } else {
      const exactAppPage = exactCurrentAppPage(connected.context, requested.id);
      if (exactAppPage) {
        appPage = exactAppPage;
        appId = requested.id;
        appPageMode = "reused_current_app_page";
      } else {
        appPage = await connected.context.newPage();
        appId = await chooseAppFromAppsPage(appPage, requested);
        appPageMode = "new_tab_same_process";
      }
    }
  }
  await openAppVersionPage(appPage, appId, displayName);
  await openMediaManager(appPage, displayName);
  const ledgerFile = path.join(sessionDir, "upload-attempt.json");
  const outcome = await uploadImages(
    appPage,
    connected.sessionId,
    appId,
    displayName,
    images,
    ledgerFile,
    downloaded.digest,
  );

  process.stdout.write("LOCAL_EDGE_SESSION=reused\n");
  process.stdout.write(`SCREENSHOT_SOURCE_FIELD=${sourceMarker}\n`);
  process.stdout.write("SCREENSHOT_PACKAGE=verified\n");
  process.stdout.write(`SCREENSHOT_ARCHIVE_EXTRACTED=${downloaded.isZip ? "verified" : "not_needed_direct_image"}\n`);
  process.stdout.write(`SCREENSHOT_FILES=verified_${images.length}\n`);
  process.stdout.write(`APP_STORE_CONNECT_APPS=${appPageMode}\n`);
  process.stdout.write("APP_IDENTITY=verified\n");
  process.stdout.write("IPHONE_69_DISPLAY=selected\n");
  process.stdout.write(`SCREENSHOT_UPLOAD=${outcome === "already_complete" ? "already_complete" : "verified"}_${images.length}_of_10\n`);
  process.stdout.write("UTM_19=verified\n");
}

main().then(() => {
  process.exit(0);
}).catch((error) => {
  process.stderr.write(`UTM_19_ERROR=${String(error.message || error).replace(/https?:\/\/\S+/g, "[redacted-url]")}\n`);
  process.exit(1);
});
