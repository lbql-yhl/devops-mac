import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash, generateKeyPairSync, verify } from 'node:crypto';
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import * as distribute from '../scripts/utm_22_distribute.mjs';

import {
  apiAuthorization,
  buildStatus,
  attemptRecoveryAction,
  classifyExistingBuilds,
  exactAppId,
  matchingBuildsQuery,
  classifyMatchingUploads,
  commitBuildUploadFileBody,
  createBuildUploadBody,
  createBuildUploadFileBody,
  makeToken,
  operationChunk,
  readDecodedProfile,
  readUploadConfiguration,
  resumePackagedIpa,
  selectArchiveCandidate,
  selectSwiftSupportLibrary,
  swiftLibraries,
  writeAttempt,
} from '../scripts/utm_22_distribute.mjs';

test('retries transient GET transport failures with bounded delays', async () => {
  assert.equal(typeof distribute.fetchWithGetRecovery, 'function');
  let attempts = 0;
  const sleeps = [];
  const response = await distribute.fetchWithGetRecovery(
    'GET',
    'https://api.appstoreconnect.apple.com/v1/apps',
    {},
    {
      fetcher: async () => {
        attempts += 1;
        if (attempts < 4) throw new Error('fetch failed');
        return { ok: true, status: 200 };
      },
      sleeper: async (delay) => sleeps.push(delay),
    },
  );

  assert.equal(response.status, 200);
  assert.equal(attempts, 4);
  assert.deepEqual(sleeps, [2_000, 5_000, 10_000]);
});

test('reads the upload configuration without reading project source files', () => {
  const root = mkdtempSync(join(tmpdir(), 'utm22-config-test-'));
  const configDirectory = join(root, 'config');
  mkdirSync(configDirectory);
  const privateKey = join(configDirectory, 'AuthKey_KEY123.p8');
  writeFileSync(privateKey, 'private-key-placeholder');
  const config = join(configDirectory, 'prod.yml');
  writeFileSync(config, `apple_store:
  issuer_id: issuer-value
  key_id: KEY123
  private_key_path: ./config/AuthKey_KEY123.p8
app_info:
  bundle_id: io.pitchpan.example
`);

  const parsed = readUploadConfiguration(config);

  assert.equal(parsed.issuerId, 'issuer-value');
  assert.equal(parsed.keyId, 'KEY123');
  assert.equal(parsed.privateKeyPath, privateKey);
  assert.equal(parsed.bundleId, 'io.pitchpan.example');
});

test('rejects a P8 file whose filename does not match the configured key ID', () => {
  const root = mkdtempSync(join(tmpdir(), 'utm22-config-mismatch-test-'));
  const configDirectory = join(root, 'config');
  mkdirSync(configDirectory);
  writeFileSync(join(configDirectory, 'AuthKey_OTHER.p8'), 'private-key-placeholder');
  const config = join(configDirectory, 'prod.yml');
  writeFileSync(config, `apple_store:
  issuer_id: issuer-value
  key_id: KEY123
  private_key_path: ./config/AuthKey_OTHER.p8
app_info:
  bundle_id: io.pitchpan.example
`);

  assert.throws(() => readUploadConfiguration(config), /P8 filename\/key ID mismatch/);
});

test('selects exactly one archive and never uses newest as a fallback', () => {
  const one = {
    path: '/Archives/PitchPan.xcarchive',
    appName: 'PitchPan',
    bundleId: 'io.pitchpan.example',
    version: '1.0.0',
    build: '1',
  };
  assert.deepEqual(selectArchiveCandidate([one], 'PitchPan', 'io.pitchpan.example'), one);
  assert.throws(
    () => selectArchiveCandidate([one, { ...one, path: '/Archives/PitchPan-2.xcarchive' }], 'PitchPan', 'io.pitchpan.example'),
    /ARCHIVE_MATCH_COUNT=2/,
  );
});

test('discovers an archive whose top-level plist contains CreationDate', () => {
  const root = mkdtempSync(join(tmpdir(), 'utm22-archive-date-test-'));
  const archiveRoot = join(root, 'Archives');
  const archive = join(archiveRoot, 'SawSet.xcarchive');
  const app = join(archive, 'Products', 'Applications', 'SawSet.app');
  mkdirSync(app, { recursive: true });
  writeFileSync(join(archive, 'Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Name</key><string>SawSet</string>
  <key>CreationDate</key><date>2026-08-05T03:34:00Z</date>
</dict></plist>`);
  writeFileSync(join(app, 'Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDisplayName</key><string>SawSet</string>
  <key>CFBundleIdentifier</key><string>com.example.sawset</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
</dict></plist>`);

  const project = join(root, 'apple-store-bm');
  const configDirectory = join(project, 'config');
  mkdirSync(configDirectory, { recursive: true });
  writeFileSync(join(configDirectory, 'AuthKey_KEY123.p8'), 'private-key-placeholder');
  const config = join(configDirectory, 'prod.yml');
  writeFileSync(config, `apple_store:
  issuer_id: issuer-value
  key_id: KEY123
  private_key_path: ./config/AuthKey_KEY123.p8
app_info:
  bundle_id: com.example.sawset
`);

  const script = fileURLToPath(new URL('../scripts/utm_22_distribute.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [
    script,
    'discover',
    '--archive-root', archiveRoot,
    '--app-name', 'SawSet',
    '--config', config,
  ], { encoding: 'utf8' });

  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stdout, /ARCHIVE_CANDIDATES_JSON=/);
  assert.match(result.stdout, /"app_name":"SawSet"/);
});

test('requires one App Store Connect app with the archive bundle ID', () => {
  assert.equal(exactAppId({
    data: [{ id: '123', attributes: { bundleId: 'io.pitchpan.example' } }],
  }, 'io.pitchpan.example'), '123');
  assert.throws(() => exactAppId({ data: [] }, 'io.pitchpan.example'), /APP_MATCH_COUNT=0/);
});

test('resumes only the persisted build upload after the file was committed', () => {
  assert.deepEqual(attemptRecoveryAction({
    state: 'file_committed',
    buildUploadId: 'upload-1',
    buildUploadFileId: 'file-1',
  }), { action: 'poll_upload', uploadId: 'upload-1' });
  assert.deepEqual(attemptRecoveryAction({
    state: 'processing',
    buildUploadId: 'upload-2',
  }), { action: 'poll_upload', uploadId: 'upload-2' });
  assert.deepEqual(attemptRecoveryAction({ state: 'preflight' }), { action: 'create' });
  assert.deepEqual(attemptRecoveryAction({
    state: 'file_reserved',
    buildUploadId: 'upload-3',
  }), { action: 'ambiguous', uploadId: 'upload-3' });
  assert.deepEqual(attemptRecoveryAction({
    state: 'existing_build_valid',
    existingBuildIds: ['build-1'],
  }), { action: 'poll_existing', buildId: 'build-1' });
  assert.deepEqual(attemptRecoveryAction({
    state: 'existing_build_processing',
    existingBuildIds: ['build-2'],
  }), { action: 'poll_existing', buildId: 'build-2' });
  assert.deepEqual(attemptRecoveryAction({
    state: 'ambiguous_existing_upload',
    existingBuildIds: ['build-1', 'build-2'],
  }), { action: 'ambiguous' });
});

test('treats one valid existing build as success and never creates again', () => {
  assert.deepEqual(classifyExistingBuilds([]), { action: 'create' });
  assert.deepEqual(classifyExistingBuilds([
    { id: 'build-1', attributes: { processingState: 'VALID' } },
  ]), { action: 'complete', buildId: 'build-1' });
  assert.deepEqual(classifyExistingBuilds([
    { id: 'build-2', attributes: { processingState: 'PROCESSING' } },
  ]), { action: 'wait', buildId: 'build-2' });
  assert.deepEqual(classifyExistingBuilds([
    { id: 'build-1', attributes: { processingState: 'VALID' } },
    { id: 'build-2', attributes: { processingState: 'PROCESSING' } },
  ]), { action: 'ambiguous', count: 2 });
});

test('rejects an embedded framework missing its marketing version before upload', () => {
  assert.equal(typeof distribute.validateEmbeddedFrameworkPlists, 'function');

  const app = join(mkdtempSync(join(tmpdir(), 'utm22-framework-plist-test-')), 'Runner.app');
  const framework = join(app, 'Frameworks', 'FBSDKCoreKit.framework');
  mkdirSync(framework, { recursive: true });
  writeFileSync(join(framework, 'Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>CFBundleIdentifier</key><string>com.example.fbcorekit</string>
    <key>CFBundleVersion</key><string>1</string>
  </dict></plist>`);

  assert.throws(
    () => distribute.validateEmbeddedFrameworkPlists(app),
    /FBSDKCoreKit\.framework.*CFBundleShortVersionString/,
  );
});

test('queries existing builds through the supported builds collection', () => {
  assert.equal(
    matchingBuildsQuery({ version: '1.2.3', build: '45' }, '1234567890'),
    '/builds?filter%5Bapp%5D=1234567890&filter%5Bversion%5D=45&include=preReleaseVersion&fields%5Bbuilds%5D=version%2CprocessingState%2CpreReleaseVersion&fields%5BpreReleaseVersions%5D=version&limit=200',
  );
});

test('writes upload attempts atomically as mode 600 and rejects symlinks', () => {
  const directory = mkdtempSync(join(tmpdir(), 'utm22-attempt-ledger-test-'));
  const attempt = join(directory, 'attempt.json');
  writeAttempt(attempt, { attemptId: 'one', state: 'prepared' });
  assert.equal(statSync(attempt).mode & 0o777, 0o600);
  assert.equal(lstatSync(attempt).isFile(), true);
  assert.equal(JSON.parse(readFileSync(attempt, 'utf8')).state, 'prepared');
  writeAttempt(attempt, { attemptId: 'one', state: 'verified' });
  assert.equal(JSON.parse(readFileSync(attempt, 'utf8')).state, 'verified');
  assert.deepEqual(readdirSync(directory), ['attempt.json']);

  const real = join(directory, 'real.json');
  const linked = join(directory, 'linked.json');
  writeFileSync(real, '{}', { mode: 0o600 });
  symlinkSync(real, linked);
  assert.throws(() => writeAttempt(linked, { attemptId: 'two' }), /symlink/);
});

test('classifies same-version build uploads before creating anything', () => {
  assert.deepEqual(classifyMatchingUploads([]), { action: 'create' });
  assert.deepEqual(classifyMatchingUploads([
    { id: 'upload-1', attributes: { state: { state: 'PROCESSING' } } },
  ]), { action: 'resume', uploadId: 'upload-1', state: 'PROCESSING' });
  assert.deepEqual(classifyMatchingUploads([
    { id: 'upload-1', attributes: { state: { state: 'COMPLETE' } } },
    { id: 'upload-2', attributes: { state: { state: 'PROCESSING' } } },
  ]), { action: 'ambiguous', count: 2 });
});

test('resumes only the exact IPA bound to the persisted attempt', () => {
  const directory = mkdtempSync(join(tmpdir(), 'utm22-attempt-ipa-test-'));
  const ipa = join(directory, 'Runner.ipa');
  const attempt = join(directory, 'attempt.json');
  writeFileSync(ipa, 'same-attempt-ipa');
  const sha256 = createHash('sha256').update('same-attempt-ipa').digest('hex');
  writeFileSync(attempt, JSON.stringify({
    appId: '123', bundleId: 'example.app', version: '1.0.0', build: '2', ipaSha256: sha256,
  }), { mode: 0o600 });
  assert.equal(resumePackagedIpa({ bundleId: 'example.app', version: '1.0.0', build: '2' }, ipa, attempt).sha256, sha256);

  const linkedIpa = join(directory, 'linked.ipa');
  const linkedAttempt = join(directory, 'linked-attempt.json');
  symlinkSync(ipa, linkedIpa);
  symlinkSync(attempt, linkedAttempt);
  assert.throws(
    () => resumePackagedIpa({ bundleId: 'example.app', version: '1.0.0', build: '2' }, linkedIpa, attempt),
    /IPA.*symlink/i,
  );
  assert.throws(
    () => resumePackagedIpa({ bundleId: 'example.app', version: '1.0.0', build: '2' }, ipa, linkedAttempt),
    /metadata.*symlink/i,
  );

  writeFileSync(ipa, 'different');
  assert.throws(
    () => resumePackagedIpa({ bundleId: 'example.app', version: '1.0.0', build: '2' }, ipa, attempt),
    /hash mismatch/,
  );
});

test('requires the related App Store Connect build to be valid', () => {
  assert.deepEqual(buildStatus({
    data: { attributes: { state: { state: 'COMPLETE' } } },
    included: [{ type: 'builds', id: 'build-id', attributes: { processingState: 'VALID', version: '1' } }],
  }), { uploadState: 'COMPLETE', buildId: 'build-id', processingState: 'VALID', version: '1' });
  assert.deepEqual(buildStatus({ data: { attributes: { state: { state: 'PROCESSING' } } } }), {
    uploadState: 'PROCESSING', buildId: undefined, processingState: undefined, version: undefined,
  });
});

test('creates the Apple build upload request bodies', () => {
  assert.deepEqual(createBuildUploadBody('1234567890', '1.2.3', '45'), {
    data: {
      type: 'buildUploads',
      attributes: {
        cfBundleShortVersionString: '1.2.3',
        cfBundleVersion: '45',
        platform: 'IOS',
      },
      relationships: { app: { data: { type: 'apps', id: '1234567890' } } },
    },
  });

  assert.deepEqual(createBuildUploadFileBody('upload-id', 'Xrimo.ipa', 1024), {
    data: {
      type: 'buildUploadFiles',
      attributes: {
        assetType: 'ASSET',
        fileName: 'Xrimo.ipa',
        fileSize: 1024,
        uti: 'com.apple.ipa',
      },
      relationships: {
        buildUpload: { data: { type: 'buildUploads', id: 'upload-id' } },
      },
    },
  });

  assert.deepEqual(commitBuildUploadFileBody('file-id'), {
    data: {
      type: 'buildUploadFiles',
      id: 'file-id',
      attributes: {
        uploaded: true,
      },
    },
  });
});

test('slices exactly the bytes requested by an upload operation', () => {
  const data = Buffer.from('0123456789');
  assert.equal(operationChunk(data, { offset: 3, length: 4 }).toString(), '3456');
  assert.throws(() => operationChunk(data, { offset: 9, length: 2 }), /outside file/);
});

test('creates a verifiable App Store Connect ES256 token', () => {
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  const token = makeToken({ issuerId: 'issuer', keyId: 'KEY123', privateKey, now: 1_700_000_000 });
  const [header, payload, signature] = token.split('.');
  assert.deepEqual(JSON.parse(Buffer.from(header, 'base64url')), {
    alg: 'ES256',
    kid: 'KEY123',
    typ: 'JWT',
  });
  assert.deepEqual(JSON.parse(Buffer.from(payload, 'base64url')), {
    iss: 'issuer',
    iat: 1_700_000_000,
    exp: 1_700_001_200,
    aud: 'appstoreconnect-v1',
  });
  assert.equal(
    verify('sha256', Buffer.from(`${header}.${payload}`), { key: publicKey, dsaEncoding: 'ieee-p1363' }, Buffer.from(signature, 'base64url')),
    true,
  );
});

test('reuses one JWT for every request in the same upload attempt', () => {
  const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  const credentials = { issuerId: 'issuer', keyId: 'KEY123', privateKey };

  const first = apiAuthorization(credentials);
  const second = apiAuthorization(credentials);

  assert.equal(first, second);
  assert.match(first, /^Bearer [^.]+\.[^.]+\.[^.]+$/);
});

test('reads required profile fields without converting certificate data to JSON', () => {
  const directory = mkdtempSync(join(tmpdir(), 'utm22-profile-test-'));
  const path = join(directory, 'profile.plist');
  writeFileSync(path, `<?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Name</key><string>Xrimo</string>
    <key>ExpirationDate</key><date>2027-07-14T07:00:28Z</date>
    <key>DeveloperCertificates</key><array><data>AQID</data></array>
    <key>TeamIdentifier</key><array><string>TEAM123</string></array>
    <key>Entitlements</key><dict>
      <key>application-identifier</key><string>TEAM123.example.app</string>
      <key>com.apple.developer.team-identifier</key><string>TEAM123</string>
      <key>get-task-allow</key><false/>
      <key>beta-reports-active</key><true/>
    </dict>
  </dict></plist>`);
  assert.deepEqual(readDecodedProfile(path), {
    name: 'Xrimo',
    expirationDate: '2027-07-14T07:00:28Z',
    applicationIdentifier: 'TEAM123.example.app',
    teamIdentifier: 'TEAM123',
    getTaskAllow: false,
    betaReportsActive: true,
    hasProvisionedDevices: false,
    provisionsAllDevices: false,
  });
});

test('selects embedded Swift runtime libraries for SwiftSupport', () => {
  const app = join(mkdtempSync(join(tmpdir(), 'utm22-swift-test-')), 'Runner.app');
  const frameworks = join(app, 'Frameworks');
  mkdirSync(frameworks, { recursive: true });
  writeFileSync(join(frameworks, 'libswift_Concurrency.dylib'), 'swift');
  writeFileSync(join(frameworks, 'App.framework'), 'not swift');
  assert.deepEqual(swiftLibraries(app).map((path) => path.split('/').at(-1)), ['libswift_Concurrency.dylib']);
});

test('accepts only the matching Apple-signed Command Line Tools Swift runtime', () => {
  const candidates = [
    { path: '/clt/right', name: 'libswift_Concurrency.dylib', uuid: 'SAME', authority: 'Software Signing' },
    { path: '/clt/wrong-version', name: 'libswift_Concurrency.dylib', uuid: 'OTHER', authority: 'Software Signing' },
    { path: '/app/resigned', name: 'libswift_Concurrency.dylib', uuid: 'SAME', authority: 'Apple Distribution: Team' },
  ];
  assert.equal(selectSwiftSupportLibrary('libswift_Concurrency.dylib', 'SAME', candidates), '/clt/right');
  assert.throws(() => selectSwiftSupportLibrary('libswift_Concurrency.dylib', 'MISSING', candidates), /exactly one/);
});
