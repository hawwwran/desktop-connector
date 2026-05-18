<?php

/**
 * Vault HTTP surface (vault_v1). One static method per endpoint, signature
 *   (Database $db, RequestContext $ctx)
 * matching the existing controller pattern (DeviceController etc.).
 *
 * Each method:
 *   1. Calls VaultAuthService::requireVaultAuth (or
 *      VaultAuthService::requireDeviceAuthForCreate for POST /api/vaults)
 *      so device + vault auth failures emit the T0 vault_v1 envelope.
 *   2. Validates the body / route params, throwing VaultInvalidRequestError.
 *   3. Delegates to repos for SQL; orchestrates filesystem writes for chunk
 *      PUT (D13 layout via VaultStorage).
 *   4. Emits the wire shape from `docs/protocol/vault-v1.md`.
 *
 * Wire shapes and idempotency rules live in vault-v1.md §6 (T1) — this
 * file is the literal-byte translation of that doc into PHP.
 */
class VaultController
{
    /**
     * Hard size caps from spec §10. Chunk envelopes are 8 MiB plaintext
     * + AEAD tag + envelope metadata; the 9 MiB ceiling absorbs
     * formatter-side variation while still failing fast on attackers
     * trying to OOM the relay. The root envelope is metadata-only
     * (4 MiB cap absorbs ≈ 50 k folder pointers before pressure);
     * folder shards carry per-folder file entries (16 MiB cap, same
     * as the pre-sharding single-manifest ceiling).
     */
    public const MAX_CHUNK_BYTES    = 9 * 1024 * 1024;
    public const MAX_ROOT_BYTES     = 4 * 1024 * 1024;
    public const MAX_SHARD_BYTES    = 16 * 1024 * 1024;
    public const MAX_HEADER_BYTES   = 64 * 1024;
    public const SUPPORTED_FORMAT_VERSION = 1;

    /** Reject vault-route bodies that exceed the spec's size envelope. */
    private static function guardEnvelopeSize(string $kind, int $observed, int $max): void
    {
        if ($observed > $max) {
            throw new VaultPayloadTooLargeError($kind, $observed, $max);
        }
    }

    /** Reject envelopes whose first byte (format_version) isn't supported. */
    private static function guardFormatVersion(string $kind, int $observedVersion): void
    {
        if ($observedVersion !== self::SUPPORTED_FORMAT_VERSION) {
            throw new VaultFormatVersionUnsupportedError($kind, $observedVersion);
        }
    }

    /** Vault-shaped requireInt — emits vault_v1 envelope on missing field. F-S04. */
    private static function vaultRequireInt(array $body, string $field): int
    {
        if (!array_key_exists($field, $body) || !is_int($body[$field])) {
            throw new VaultInvalidRequestError(
                "{$field} is required and must be an integer", $field,
            );
        }
        return (int) $body[$field];
    }

    /** Vault-shaped requireNonEmptyString — emits vault_v1 envelope on missing field. F-S04. */
    private static function vaultRequireNonEmptyString(array $body, string $field): string
    {
        if (!array_key_exists($field, $body) || !is_string($body[$field]) || $body[$field] === '') {
            throw new VaultInvalidRequestError(
                "{$field} is required and must be a non-empty string", $field,
            );
        }
        return (string) $body[$field];
    }

    /**
     * Review §1.M1 — `header_hash`, `root_hash`, `shard_hash` MUST match
     * `^[a-f0-9]{64}$`. Pre-fix vaultRequireNonEmptyString accepted any
     * non-empty string and "banana" would survive storage to surface in
     * 409 `current_*_hash` payloads as opaque garbage. The §10.C decrypt
     * checks still catch this on the desktop side, but a client that
     * trusts the relay's 409 reply (e.g. to compare hash-equality for
     * idempotency) needs the hash bytes to actually be hex.
     */
    private static function vaultRequireHex64(array $body, string $field): string
    {
        $raw = self::vaultRequireNonEmptyString($body, $field);
        if (!preg_match('/^[a-f0-9]{64}$/D', $raw)) {
            throw new VaultInvalidRequestError(
                "{$field} must be 64 lowercase hex chars (SHA-256 digest)", $field,
            );
        }
        return $raw;
    }

    /** Server-side timestamp serialization (UTC, second precision, RFC 3339). */
    private static function ts(int $epoch): string
    {
        return gmdate('Y-m-d\TH:i:s\Z', $epoch);
    }

    /** Internal canonical (undashed, uppercase) → display form (4-4-4 dashed). */
    private static function dashedVaultId(string $undashed): string
    {
        return substr($undashed, 0, 4) . '-' . substr($undashed, 4, 4) . '-' . substr($undashed, 8, 4);
    }

    /** Strip dashes, uppercase, validate. Throws on malformed id. */
    private static function normalizeVaultId(string $any): string
    {
        $stripped = strtoupper(str_replace('-', '', $any));
        if (!preg_match('/^[A-Z2-7]{12}$/', $stripped)) {
            throw new VaultInvalidRequestError('vault_id must be 12 base32 characters', 'vault_id');
        }
        return $stripped;
    }

    /**
     * base64-decode with strict mode + length check; throws on either failure.
     * Pre-check enforces RFC 4648 §4 alphabet (`+/`, padding kept) — URL-safe
     * `-_` is rejected explicitly so a wire-byte hash test that compares against
     * the spec'd alphabet doesn't quietly diverge.
     */
    private static function decodeBase64Field(array $body, string $field, ?int $expectedLength = null): string
    {
        $b64 = Validators::requireNonEmptyString($body, $field);
        if (!preg_match('#^[A-Za-z0-9+/]*={0,2}$#', $b64)) {
            throw new VaultInvalidRequestError("{$field} is not valid base64", $field);
        }
        $raw = base64_decode($b64, true);
        if ($raw === false) {
            throw new VaultInvalidRequestError("{$field} is not valid base64", $field);
        }
        if ($expectedLength !== null && strlen($raw) !== $expectedLength) {
            throw new VaultInvalidRequestError(
                "{$field} must decode to {$expectedLength} bytes",
                $field
            );
        }
        // Review §1.M6 — reject empty-payload base64 when no expected
        // length is set. ``"=="`` is valid RFC4648 input but decodes to
        // zero bytes, which has no place as a vault envelope / wrapped
        // grant / encrypted blob.
        if ($expectedLength === null && strlen($raw) === 0) {
            throw new VaultInvalidRequestError(
                "{$field} must decode to at least 1 byte",
                $field
            );
        }
        return $raw;
    }

    // ===================================================================
    //  6.1  POST /api/vaults — create
    // ===================================================================

    public static function create(Database $db, RequestContext $ctx): void
    {
        $deviceId = VaultAuthService::requireDeviceAuthForCreate($db);

        $body = $ctx->jsonBody();
        $vaultId = self::normalizeVaultId(self::vaultRequireNonEmptyString($body, 'vault_id'));

        $tokenHash = self::decodeBase64Field($body, 'vault_access_token_hash', 32);
        $encHeader = self::decodeBase64Field($body, 'encrypted_header');
        self::guardEnvelopeSize('header', strlen($encHeader), self::MAX_HEADER_BYTES);
        $headerHash = self::vaultRequireHex64($body, 'header_hash');

        // Sharded create — the only path. The pre-sharding
        // ``initial_manifest_*`` shape was retired alongside the
        // ``vault_manifests`` table and the ``/manifest`` endpoints.
        $rootCipher = self::decodeBase64Field($body, 'initial_root_ciphertext');
        self::guardEnvelopeSize('root', strlen($rootCipher), self::MAX_ROOT_BYTES);
        $rootHash = self::vaultRequireHex64($body, 'initial_root_hash');
        $initialRootRevision = isset($body['initial_root_revision'])
            ? self::vaultRequireInt($body, 'initial_root_revision') : 1;
        if ($initialRootRevision < 1) {
            throw new VaultInvalidRequestError(
                'initial_root_revision must be >= 1',
                'initial_root_revision'
            );
        }

        // F-S11: persist the recovery-derived purge_token_hash so the
        // T14 hard-purge path can authenticate `purge_secret`. Optional
        // for older clients that haven't been migrated yet.
        $purgeTokenHash = null;
        if (isset($body['purge_token_hash'])) {
            $purgeTokenHash = self::decodeBase64Field($body, 'purge_token_hash', 32);
        }

        $initialHeaderRevision = isset($body['initial_header_revision'])
            ? self::vaultRequireInt($body, 'initial_header_revision') : 1;
        if ($initialHeaderRevision < 1) {
            throw new VaultInvalidRequestError(
                'initial_header_revision must be >= 1',
                'initial_header_revision'
            );
        }

        // Review §1.H4: every OTHER endpoint that ingests a header or
        // root envelope parses its deterministic prefix, runs
        // ``guardFormatVersion`` (refuse v2 envelopes before AEAD
        // attempt), and confirms the envelope's sealed (vault_id,
        // revision) matches the request body. ``create`` skipped all
        // three — a malformed envelope or an envelope/body mismatch
        // could be persisted, and a v2-bumped envelope wouldn't 422
        // before storage. Same checks applied here:
        try {
            $envHeader = VaultCrypto::parseHeaderEnvelopeHeader($encHeader);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'encrypted_header');
        }
        self::guardFormatVersion('header', (int) $envHeader['format_version']);
        if ($envHeader['vault_id'] !== $vaultId) {
            throw new VaultHeaderTamperedError(
                'encrypted_header envelope vault_id does not match path vault_id',
            );
        }
        if ((int) $envHeader['header_revision'] !== $initialHeaderRevision) {
            throw new VaultHeaderTamperedError(
                'encrypted_header envelope header_revision does not match initial_header_revision',
            );
        }
        try {
            $envRoot = VaultCrypto::parseRootEnvelopeHeader($rootCipher);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'initial_root_ciphertext');
        }
        self::guardFormatVersion('root', (int) $envRoot['format_version']);
        if ($envRoot['vault_id'] !== $vaultId) {
            throw new VaultRootTamperedError(
                'initial_root_ciphertext envelope vault_id does not match path vault_id',
            );
        }
        if ((int) $envRoot['root_revision'] !== $initialRootRevision) {
            throw new VaultRootTamperedError(
                'initial_root_ciphertext envelope root_revision does not match initial_root_revision',
            );
        }

        $vaultsRepo = new VaultsRepository($db);
        if ($vaultsRepo->getById($vaultId) !== null) {
            throw new VaultAlreadyExistsError($vaultId);
        }

        $now = time();
        $vaultsRepo->create(
            $vaultId, $tokenHash, $encHeader, $headerHash, $rootHash, $now,
            $initialHeaderRevision, $initialRootRevision, $purgeTokenHash,
        );

        $rootRepo = new VaultRootManifestsRepository($db);
        $rootRepo->create(
            $vaultId,
            $initialRootRevision,
            $initialRootRevision - 1,
            $rootHash,
            $rootCipher,
            strlen($rootCipher),
            $deviceId,
            $now
        );

        // §D11: the creating device is the genesis admin. Insert the grant
        // here so subsequent role-gated writes from this device pass without
        // a separate provisioning step.
        $grants = new VaultDeviceGrantsRepository($db);
        if ($grants->getByDevice($vaultId, $deviceId) === null) {
            $grants->insertGrant(
                self::generateGrantId(),
                $vaultId,
                $deviceId,
                null,
                'admin',
                $deviceId,
                'create',
                $now,
            );
        }

        // F-S18: emit the actual stored quota — never a hardcoded value.
        $persistedVault = $vaultsRepo->getById($vaultId);
        $quotaBytes = $persistedVault !== null
            ? (int) ($persistedVault['quota_ciphertext_bytes'] ?? 0)
            : 0;

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'               => self::dashedVaultId($vaultId),
                'header_revision'        => $initialHeaderRevision,
                'root_revision'          => $initialRootRevision,
                'quota_ciphertext_bytes' => $quotaBytes,
                'used_ciphertext_bytes'  => 0,
                'created_at'             => self::ts($now),
            ],
        ], 201);
    }

    // ===================================================================
    //  6.2  GET /api/vaults/{vault_id}/header
    // ===================================================================

    public static function getHeader(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        // Review §6.C5: surface the caller's role so the desktop can
        // disable the Schedule-purge button (and any other admin-only
        // op) when the device isn't admin. /device-grants is itself
        // admin-gated so a sync-only device cannot discover its role
        // through that endpoint; getHeader is callable by every
        // granted role, making it the natural carrier for the field.
        $callerDevice = (string)($ctx->deviceId ?? '');
        $callerRole = null;
        if ($callerDevice !== '') {
            $grants = new VaultDeviceGrantsRepository($db);
            $grant = $grants->getByDevice($vaultId, $callerDevice);
            if ($grant !== null && $grant['revoked_at'] === null) {
                $callerRole = (string)$grant['role'];
            }
        }

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'               => self::dashedVaultId($vaultId),
                'encrypted_header'       => base64_encode((string)$vault['encrypted_header']),
                'header_hash'            => (string)$vault['header_hash'],
                'header_revision'        => (int)$vault['header_revision'],
                'quota_ciphertext_bytes' => (int)$vault['quota_ciphertext_bytes'],
                'used_ciphertext_bytes'  => (int)$vault['used_ciphertext_bytes'],
                'migrated_to'            => $vault['migrated_to'] !== null ? (string)$vault['migrated_to'] : null,
                'caller_role'            => $callerRole,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.3  PUT /api/vaults/{vault_id}/header (CAS)
    // ===================================================================

    public static function putHeader(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'admin');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $expected = self::vaultRequireInt($body, 'expected_header_revision');
        $newRev   = self::vaultRequireInt($body, 'new_header_revision');
        if ($newRev !== $expected + 1) {
            throw new VaultInvalidRequestError(
                'new_header_revision must be expected_header_revision + 1',
                'new_header_revision'
            );
        }
        $encHeader  = self::decodeBase64Field($body, 'encrypted_header');
        $headerHash = self::vaultRequireHex64($body, 'header_hash');
        self::guardEnvelopeSize('header', strlen($encHeader), self::MAX_HEADER_BYTES);

        // Authoritatively read (vault_id, header_revision) from the
        // envelope's deterministic prefix and reject if it disagrees with
        // the path / body. Forces the body's claims to match the envelope's
        // sealed AAD — without this, a buggy admin client whose envelope
        // and body diverge would silently poison the chain.
        try {
            $envHeader = VaultCrypto::parseHeaderEnvelopeHeader($encHeader);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError(
                $e->getMessage(),
                'encrypted_header'
            );
        }
        // F-S09: format-version validation before commit.
        self::guardFormatVersion('header', (int) $envHeader['format_version']);
        if ($envHeader['vault_id'] !== $vaultId) {
            throw new VaultHeaderTamperedError(
                'encrypted_header envelope vault_id does not match path vault_id',
            );
        }
        if ($envHeader['header_revision'] !== $newRev) {
            throw new VaultHeaderTamperedError(
                'encrypted_header envelope header_revision does not match new_header_revision',
            );
        }

        $vaultsRepo = new VaultsRepository($db);
        $now = time();
        $ok = $vaultsRepo->setHeaderCiphertext($vaultId, $encHeader, $headerHash, $expected, $now);
        if (!$ok) {
            // Re-read the current revision so the client knows where head is.
            $current = $vaultsRepo->getById($vaultId);
            throw new VaultManifestConflictError([
                'current_revision'  => (int)$current['header_revision'],
                'expected_revision' => $expected,
            ], 'The vault header changed on the server.');
        }

        Router::json([
            'ok' => true,
            'data' => [
                'header_revision' => $newRev,
                'header_hash'     => $headerHash,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.4  GET /api/vaults/{vault_id}/root
    // ===================================================================

    public static function getRoot(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $rootRepo = new VaultRootManifestsRepository($db);
        $current = $rootRepo->getCurrent($vaultId);
        if ($current === null) {
            // A vault that exists but has no root is malformed; surface
            // as not-found rather than 500.
            throw new VaultNotFoundError($vaultId);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'root_revision'        => (int)$current['root_revision'],
                'parent_root_revision' => (int)$current['parent_root_revision'],
                'root_hash'            => (string)$current['root_hash'],
                'root_ciphertext'      => base64_encode((string)$current['root_ciphertext']),
                'root_size'            => (int)$current['root_size'],
            ],
        ], 200);
    }

    // ===================================================================
    //  6.5  GET /api/vaults/{vault_id}/folders/{folder_id}/shard
    // ===================================================================

    public static function getShard(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $folderId = self::normalizeRemoteFolderId((string)($ctx->params['folder_id'] ?? ''));
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $shardsRepo = new VaultFolderShardsRepository($db);
        $current = $shardsRepo->getCurrent($vaultId, $folderId);
        if ($current === null) {
            throw new VaultNotFoundError("shard {$folderId} for {$vaultId}");
        }

        Router::json([
            'ok' => true,
            'data' => [
                'remote_folder_id'      => (string)$current['remote_folder_id'],
                'shard_revision'        => (int)$current['shard_revision'],
                'parent_shard_revision' => (int)$current['parent_shard_revision'],
                'shard_hash'            => (string)$current['shard_hash'],
                'shard_ciphertext'      => base64_encode((string)$current['shard_ciphertext']),
                'shard_size'            => (int)$current['shard_size'],
            ],
        ], 200);
    }

    // ===================================================================
    //  6.6  PUT /api/vaults/{vault_id}/root (CAS, §A1-root conflict)
    // ===================================================================

    public static function putRoot(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'browse-upload');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $expected   = self::vaultRequireInt($body, 'expected_current_root_revision');
        $newRev     = self::vaultRequireInt($body, 'new_root_revision');
        $parentRev  = self::vaultRequireInt($body, 'parent_root_revision');
        $rootHash   = self::vaultRequireHex64($body, 'root_hash');
        $rootCipher = self::decodeBase64Field($body, 'root_ciphertext');
        self::guardEnvelopeSize('root', strlen($rootCipher), self::MAX_ROOT_BYTES);

        if ($parentRev !== $expected) {
            throw new VaultInvalidRequestError(
                'parent_root_revision must equal expected_current_root_revision',
                'parent_root_revision'
            );
        }
        if ($newRev !== $expected + 1) {
            throw new VaultInvalidRequestError(
                'new_root_revision must be expected_current_root_revision + 1',
                'new_root_revision'
            );
        }

        $rootRepo = new VaultRootManifestsRepository($db);
        $authorDeviceId = (string)($ctx->deviceId ?? '');

        // F-S09 + envelope/body consistency check — same shape as the
        // legacy putManifest path. Reading from envelope bytes makes
        // body/envelope drift bugs surface as 422 rather than poisoning
        // the chain.
        try {
            $envRoot = VaultCrypto::parseRootEnvelopeHeader($rootCipher);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'root_ciphertext');
        }
        self::guardFormatVersion('root', (int) $envRoot['format_version']);
        if ($envRoot['vault_id'] !== $vaultId) {
            throw new VaultRootTamperedError(
                'root envelope vault_id does not match path vault_id',
            );
        }
        if ($envRoot['root_revision'] !== $newRev) {
            throw new VaultRootTamperedError(
                'root envelope root_revision does not match new_root_revision',
            );
        }
        if ($envRoot['parent_root_revision'] !== $parentRev) {
            throw new VaultRootTamperedError(
                'root envelope parent_root_revision does not match body parent_root_revision',
            );
        }
        if ($authorDeviceId !== '' && $envRoot['author_device_id'] !== $authorDeviceId) {
            throw new VaultRootTamperedError(
                'root envelope author_device_id does not match X-Device-ID',
            );
        }

        $now = time();
        $conflict = $rootRepo->tryCAS(
            $vaultId,
            $expected,
            $newRev,
            $rootHash,
            $rootCipher,
            strlen($rootCipher),
            $authorDeviceId,
            $now
        );
        if ($conflict !== null) {
            throw new VaultRootConflictError([
                'current_root_revision'    => (int)$conflict['current_root_revision'],
                'expected_root_revision'   => $expected,
                'current_root_hash'        => (string)$conflict['current_root_hash'],
                'current_root_ciphertext'  => base64_encode((string)$conflict['current_root_ciphertext']),
                'current_root_size'        => (int)$conflict['current_root_size'],
            ]);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'root_revision' => $newRev,
                'root_hash'     => $rootHash,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.7  PUT /api/vaults/{vault_id}/folders/{folder_id}/shard
    //         (CAS, §A1-shard conflict)
    // ===================================================================

    public static function putShard(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $folderId = self::normalizeRemoteFolderId((string)($ctx->params['folder_id'] ?? ''));
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'browse-upload');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $expected    = self::vaultRequireInt($body, 'expected_current_shard_revision');
        $newRev      = self::vaultRequireInt($body, 'new_shard_revision');
        $parentRev   = self::vaultRequireInt($body, 'parent_shard_revision');
        $shardHash   = self::vaultRequireHex64($body, 'shard_hash');
        $shardCipher = self::decodeBase64Field($body, 'shard_ciphertext');
        self::guardEnvelopeSize('shard', strlen($shardCipher), self::MAX_SHARD_BYTES);

        // Envelope chain integrity: the new envelope's stated parent
        // must be exactly one less than its revision. Normal edits
        // satisfy this because the client publishes against its known
        // server head (expected = parent = new - 1). The relay-migration
        // path replicates source shards verbatim into a fresh target
        // (expected = 0, parent = source.parent, new = source.revision),
        // so we don't conflate the CAS atomicity check (expected == server's
        // current revision, enforced atomically by tryCAS) with the
        // envelope chain check (parent == new - 1, enforced here).
        if ($newRev < 1) {
            throw new VaultInvalidRequestError(
                'new_shard_revision must be >= 1', 'new_shard_revision'
            );
        }
        if ($parentRev !== $newRev - 1) {
            throw new VaultInvalidRequestError(
                'parent_shard_revision must equal new_shard_revision - 1',
                'parent_shard_revision'
            );
        }

        $shardsRepo = new VaultFolderShardsRepository($db);
        $authorDeviceId = (string)($ctx->deviceId ?? '');

        // §5.M2: genesis insert path (expected=0) is also the
        // migration-replication path — permit envelopes authored by a
        // peer device. Normal first-folder publishes are unaffected
        // because their envelope is authored by this device anyway.
        self::validateShardEnvelopeAgainstBody(
            $shardCipher, $vaultId, $folderId, $newRev, $parentRev, $authorDeviceId,
            $expected === 0,
        );

        $now = time();
        $conflict = $shardsRepo->tryCAS(
            $vaultId,
            $folderId,
            $expected,
            $newRev,
            $parentRev,
            $shardHash,
            $shardCipher,
            strlen($shardCipher),
            $authorDeviceId,
            $now
        );
        if ($conflict !== null) {
            throw new VaultShardConflictError([
                'remote_folder_id'         => (string)$conflict['remote_folder_id'],
                'current_shard_revision'   => (int)$conflict['current_shard_revision'],
                'expected_shard_revision'  => $expected,
                'current_shard_hash'       => (string)$conflict['current_shard_hash'],
                'current_shard_ciphertext' => base64_encode((string)$conflict['current_shard_ciphertext']),
                'current_shard_size'       => (int)$conflict['current_shard_size'],
            ]);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'shard_revision' => $newRev,
                'shard_hash'     => $shardHash,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.8  PUT /api/vaults/{vault_id}/folders/{folder_id}/shard-with-root
    //         (atomic shard + root CAS — primary publish path)
    // ===================================================================

    public static function putShardWithRoot(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $folderId = self::normalizeRemoteFolderId((string)($ctx->params['folder_id'] ?? ''));
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'browse-upload');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        if (!isset($body['shard']) || !is_array($body['shard'])) {
            throw new VaultInvalidRequestError('shard is required and must be an object', 'shard');
        }
        if (!isset($body['root']) || !is_array($body['root'])) {
            throw new VaultInvalidRequestError('root is required and must be an object', 'root');
        }
        $shardBody = $body['shard'];
        $rootBody  = $body['root'];

        $expectedShardRev = self::vaultRequireInt($shardBody, 'expected_current_shard_revision');
        $newShardRev      = self::vaultRequireInt($shardBody, 'new_shard_revision');
        $parentShardRev   = self::vaultRequireInt($shardBody, 'parent_shard_revision');
        $shardHash        = self::vaultRequireHex64($shardBody, 'shard_hash');
        $shardCipher      = self::decodeBase64Field($shardBody, 'shard_ciphertext');
        self::guardEnvelopeSize('shard', strlen($shardCipher), self::MAX_SHARD_BYTES);

        $expectedRootRev = self::vaultRequireInt($rootBody, 'expected_current_root_revision');
        $newRootRev      = self::vaultRequireInt($rootBody, 'new_root_revision');
        $parentRootRev   = self::vaultRequireInt($rootBody, 'parent_root_revision');
        $rootHash        = self::vaultRequireHex64($rootBody, 'root_hash');
        $rootCipher      = self::decodeBase64Field($rootBody, 'root_ciphertext');
        self::guardEnvelopeSize('root', strlen($rootCipher), self::MAX_ROOT_BYTES);

        if ($parentShardRev !== $expectedShardRev || $newShardRev !== $expectedShardRev + 1) {
            throw new VaultInvalidRequestError(
                'shard revision pair invariant violated', 'shard',
            );
        }
        if ($parentRootRev !== $expectedRootRev || $newRootRev !== $expectedRootRev + 1) {
            throw new VaultInvalidRequestError(
                'root revision pair invariant violated', 'root',
            );
        }

        $authorDeviceId = (string)($ctx->deviceId ?? '');
        // §5.M2: same author-mismatch relaxation for putShardWithRoot's
        // genesis-insert path — first publish of a new folder may
        // also arrive via migration replication (atomic publish of a
        // shard + root pair). Normal first-publishes still pass the
        // strict check because their envelopes are this-device-authored.
        self::validateShardEnvelopeAgainstBody(
            $shardCipher, $vaultId, $folderId, $newShardRev, $parentShardRev, $authorDeviceId,
            $expectedShardRev === 0,
        );
        self::validateRootEnvelopeAgainstBody(
            $rootCipher, $vaultId, $newRootRev, $parentRootRev, $authorDeviceId,
        );

        $shardsRepo = new VaultFolderShardsRepository($db);
        $rootRepo   = new VaultRootManifestsRepository($db);
        $now = time();

        $conflict = $shardsRepo->tryAtomicShardWithRootCAS(
            vaultId: $vaultId,
            remoteFolderId: $folderId,
            expectedCurrentShardRevision: $expectedShardRev,
            newShardRevision: $newShardRev,
            shardHash: $shardHash,
            shardCiphertext: $shardCipher,
            shardSize: strlen($shardCipher),
            expectedCurrentRootRevision: $expectedRootRev,
            newRootRevision: $newRootRev,
            rootHash: $rootHash,
            rootCiphertext: $rootCipher,
            rootSize: strlen($rootCipher),
            authorDeviceId: $authorDeviceId,
            now: $now,
            rootRepo: $rootRepo,
        );

        if ($conflict !== null) {
            $kind = (string)($conflict['kind'] ?? '');
            if ($kind === 'shard_root') {
                $sc = $conflict['shard'];
                $rc = $conflict['root'];
                throw new VaultShardRootConflictError([
                    'remote_folder_id'         => (string)$sc['remote_folder_id'],
                    'current_shard_revision'   => (int)$sc['current_shard_revision'],
                    'expected_shard_revision'  => $expectedShardRev,
                    'current_shard_hash'       => (string)$sc['current_shard_hash'],
                    'current_shard_ciphertext' => base64_encode((string)$sc['current_shard_ciphertext']),
                    'current_shard_size'       => (int)$sc['current_shard_size'],
                    'current_root_revision'    => (int)$rc['current_root_revision'],
                    'expected_root_revision'   => $expectedRootRev,
                    'current_root_hash'        => (string)$rc['current_root_hash'],
                    'current_root_ciphertext'  => base64_encode((string)$rc['current_root_ciphertext']),
                    'current_root_size'        => (int)$rc['current_root_size'],
                ]);
            }
            if ($kind === 'shard') {
                $sc = $conflict['shard'];
                throw new VaultShardConflictError([
                    'remote_folder_id'         => (string)$sc['remote_folder_id'],
                    'current_shard_revision'   => (int)$sc['current_shard_revision'],
                    'expected_shard_revision'  => $expectedShardRev,
                    'current_shard_hash'       => (string)$sc['current_shard_hash'],
                    'current_shard_ciphertext' => base64_encode((string)$sc['current_shard_ciphertext']),
                    'current_shard_size'       => (int)$sc['current_shard_size'],
                ]);
            }
            // root-only
            $rc = $conflict['root'];
            throw new VaultRootConflictError([
                'current_root_revision'    => (int)$rc['current_root_revision'],
                'expected_root_revision'   => $expectedRootRev,
                'current_root_hash'        => (string)$rc['current_root_hash'],
                'current_root_ciphertext'  => base64_encode((string)$rc['current_root_ciphertext']),
                'current_root_size'        => (int)$rc['current_root_size'],
            ]);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'shard_revision' => $newShardRev,
                'shard_hash'     => $shardHash,
                'root_revision'  => $newRootRev,
                'root_hash'      => $rootHash,
            ],
        ], 200);
    }

    /**
     * Common envelope-vs-body consistency check for shard publishes.
     * Mirrors the legacy ``putManifest`` envelope-header validation
     * shape so envelope/body drift surfaces as a 422 before any DB
     * write touches the shard chain.
     */
    private static function validateShardEnvelopeAgainstBody(
        string $shardCipher,
        string $vaultId,
        string $folderId,
        int $newShardRev,
        int $parentShardRev,
        string $authorDeviceId,
        bool $allowEnvelopeAuthorMismatch = false,
    ): void {
        try {
            $env = VaultCrypto::parseShardEnvelopeHeader($shardCipher);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'shard_ciphertext');
        }
        self::guardFormatVersion('shard', (int) $env['format_version']);
        if ($env['vault_id'] !== $vaultId) {
            throw new VaultShardTamperedError(
                'shard envelope vault_id does not match path vault_id',
            );
        }
        if ($env['remote_folder_id'] !== $folderId) {
            throw new VaultShardTamperedError(
                'shard envelope remote_folder_id does not match path folder_id',
            );
        }
        if ($env['shard_revision'] !== $newShardRev) {
            throw new VaultShardTamperedError(
                'shard envelope shard_revision does not match new_shard_revision',
            );
        }
        if ($env['parent_shard_revision'] !== $parentShardRev) {
            throw new VaultShardTamperedError(
                'shard envelope parent_shard_revision does not match body parent_shard_revision',
            );
        }
        // §5.M2 — Migration replication replays a source-side shard
        // envelope verbatim onto a fresh target. The envelope's
        // author_device_id is whichever peer wrote it on the source,
        // not the migrating device. ``$allowEnvelopeAuthorMismatch``
        // is set by the genesis-insert call path
        // (``expected_current_shard_revision === 0``) so the migration
        // doesn't fail on the first shard authored by a peer.
        //
        // For normal first-publishes the envelope's author is this
        // device anyway, so the relaxation is invisible there. The
        // server-side access control (vault-bearer + X-Device-ID +
        // per-role check) is unaffected — ``author_device_id`` is
        // metadata, not a security boundary (all paired devices
        // share master_key and can already construct any envelope
        // they want).
        if (
            !$allowEnvelopeAuthorMismatch
            && $authorDeviceId !== ''
            && $env['author_device_id'] !== $authorDeviceId
        ) {
            throw new VaultShardTamperedError(
                'shard envelope author_device_id does not match X-Device-ID',
            );
        }
    }

    /**
     * Common envelope-vs-body consistency check for root publishes.
     */
    private static function validateRootEnvelopeAgainstBody(
        string $rootCipher,
        string $vaultId,
        int $newRootRev,
        int $parentRootRev,
        string $authorDeviceId,
    ): void {
        try {
            $env = VaultCrypto::parseRootEnvelopeHeader($rootCipher);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'root_ciphertext');
        }
        self::guardFormatVersion('root', (int) $env['format_version']);
        if ($env['vault_id'] !== $vaultId) {
            throw new VaultRootTamperedError(
                'root envelope vault_id does not match path vault_id',
            );
        }
        if ($env['root_revision'] !== $newRootRev) {
            throw new VaultRootTamperedError(
                'root envelope root_revision does not match new_root_revision',
            );
        }
        if ($env['parent_root_revision'] !== $parentRootRev) {
            throw new VaultRootTamperedError(
                'root envelope parent_root_revision does not match body parent_root_revision',
            );
        }
        if ($authorDeviceId !== '' && $env['author_device_id'] !== $authorDeviceId) {
            throw new VaultRootTamperedError(
                'root envelope author_device_id does not match X-Device-ID',
            );
        }
    }

    /** Validate + normalize an rf_v1_<24base32> remote folder id from the URL. */
    private static function normalizeRemoteFolderId(string $raw): string
    {
        if (!preg_match('/^rf_v1_[a-z2-7]{24}$/', $raw)) {
            throw new VaultInvalidRequestError(
                'folder_id must match ^rf_v1_[a-z2-7]{24}$', 'folder_id',
            );
        }
        return $raw;
    }

    // ===================================================================
    //  6.8  PUT /api/vaults/{vault_id}/chunks/{chunk_id}
    // ===================================================================

    public static function putChunk(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $chunkId = (string)($ctx->params['chunk_id'] ?? '');
        if (!VaultChunksRepository::isValidChunkId($chunkId)) {
            throw new VaultInvalidRequestError("chunk_id '{$chunkId}' fails ^ch_v1_[a-z2-7]{24}\$", 'chunk_id');
        }
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'browse-upload');
        self::guardReadOnly($vault);

        $bytes = $ctx->rawBody();
        $size  = strlen($bytes);
        if ($size === 0) {
            throw new VaultInvalidRequestError('chunk body is empty', 'body');
        }
        self::guardEnvelopeSize('chunk', $size, self::MAX_CHUNK_BYTES);
        // Chunk envelopes have no format-version byte — chunk format is
        // pinned by the ``ch_v1_…`` chunk_id namespace (formats §11.1);
        // a v2 chunk uses ``ch_v2_…`` and is rejected at routing.
        $hash = hash('sha256', $bytes);

        $chunksRepo = new VaultChunksRepository($db);
        $vaultsRepo = new VaultsRepository($db);

        $relativePath = VaultChunksRepository::storagePath($vaultId, $chunkId);
        $now = time();

        // Atomic head + quota-reserve + insert. SQLite serializes via
        // BEGIN IMMEDIATE so two parallel uploads can't each pass a
        // preflight check and over-allocate the cap (TOCTOU fix). The
        // disk-write happens AFTER commit so a long fsync doesn't hold
        // the writer slot; failure backs out the reservation below.
        $db->execute('BEGIN IMMEDIATE');
        $result = null;
        try {
            // Review §1.C1: a purged row had its bytes freed at gc/execute
            // time, so a re-upload must re-reserve quota even though the row
            // metadata still exists. Active/retained/gc_pending rows keep
            // their bytes accounted; only a missing row OR a purged row
            // counts as fresh allocation here.
            $existing = $chunksRepo->head($vaultId, $chunkId);
            $needsReserve = (
                $existing === null
                || (string)$existing['state'] === VaultChunksRepository::STATE_PURGED
            );
            if ($needsReserve) {
                if (!$vaultsRepo->reserveCiphertextBytes($vaultId, $size, $now)) {
                    $db->execute('ROLLBACK');
                    $vault = $vaultsRepo->getById($vaultId);
                    throw new VaultQuotaExceededError(
                        (int)($vault['used_ciphertext_bytes'] ?? 0),
                        (int)($vault['quota_ciphertext_bytes'] ?? 0),
                        false
                    );
                }
            }
            try {
                $result = $chunksRepo->put($vaultId, $chunkId, $hash, $size, $relativePath, $now);
            } catch (VaultChunkSizeMismatchException $e) {
                $db->execute('ROLLBACK');
                $existing = $chunksRepo->head($vaultId, $chunkId);
                throw new VaultChunkSizeMismatchError(
                    $chunkId,
                    $existing !== null ? (int)$existing['ciphertext_size'] : 0,
                    $size
                );
            } catch (VaultChunkTamperedException $e) {
                $db->execute('ROLLBACK');
                $existing = $chunksRepo->head($vaultId, $chunkId);
                throw new VaultChunkTamperedError(
                    $chunkId,
                    $existing !== null ? (string)$existing['chunk_hash'] : '',
                    $hash
                );
            }
            $db->execute('COMMIT');
        } catch (VaultApiError $e) {
            // Already-formatted error — rollback (if not already done) and
            // re-throw. Repeated rollback on a closed tx is a no-op.
            try { $db->execute('ROLLBACK'); } catch (\Throwable $ignored) {}
            throw $e;
        } catch (\Throwable $e) {
            $db->execute('ROLLBACK');
            throw $e;
        }

        $statusCode = 201;
        if ($result === 'created') {
            $absPath = VaultStorage::chunkAbsolutePath($vaultId, $chunkId);
            VaultStorage::ensureDir($absPath);
            // Atomic write: first to a temp sibling, then rename. On
            // any failure we delete the chunk row AND release the
            // bytes — leaving neither half-state behind. F-S02.
            $tempPath = $absPath . '.part-' . bin2hex(random_bytes(6));
            $tempOk = @file_put_contents($tempPath, $bytes);
            $writeOk = ($tempOk !== false && $tempOk === $size);
            $renameOk = false;
            if ($writeOk) {
                $renameOk = @rename($tempPath, $absPath);
            }
            if (!$writeOk || !$renameOk) {
                if (is_file($tempPath)) {
                    @unlink($tempPath);
                }
                // Review §1.H6: the row-delete and bytes-decrement
                // must land atomically. Pre-fix the two writes ran
                // sequentially after the outer COMMIT, so a crash
                // between them left a permanent (vaults.used_bytes,
                // chunk_count) skew: the row was gone but the bytes
                // stayed counted. Wrap in a writer transaction so
                // the pair either both commit or both roll back.
                $db->execute('BEGIN IMMEDIATE');
                try {
                    $chunksRepo->deleteRow($vaultId, $chunkId);
                    $vaultsRepo->incUsedBytes($vaultId, -$size, -1, $now);
                    $db->execute('COMMIT');
                } catch (\Throwable $rollbackExc) {
                    try { $db->execute('ROLLBACK'); } catch (\Throwable $ignored) {}
                    // Surface the underlying storage error to the
                    // caller; the inner rollback failure is logged
                    // for operator visibility but doesn't replace
                    // the user-facing 5xx.
                    AppLog::log('vault', sprintf(
                        'vault.chunk.rollback_failed chunk=%s err=%s',
                        substr($chunkId, 0, 12),
                        $rollbackExc->getMessage(),
                    ), 'error');
                }
                throw new VaultStorageUnavailableError("Failed to write chunk to {$relativePath}");
            }
        } else {
            // Idempotent no-op: row already there + last_referenced_at bumped.
            $statusCode = 200;
        }

        Router::json([
            'ok' => true,
            'data' => [
                'chunk_id' => $chunkId,
                'size'     => $size,
                'stored'   => true,
            ],
        ], $statusCode);
    }

    // ===================================================================
    //  6.9  GET /api/vaults/{vault_id}/chunks/{chunk_id}
    // ===================================================================

    public static function getChunk(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $chunkId = (string)($ctx->params['chunk_id'] ?? '');
        if (!VaultChunksRepository::isValidChunkId($chunkId)) {
            throw new VaultInvalidRequestError('chunk_id format invalid', 'chunk_id');
        }
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $chunksRepo = new VaultChunksRepository($db);
        $row = $chunksRepo->get($vaultId, $chunkId);
        // Review §1.H2: state-gate the response. headChunk and
        // batchHead both filter via ``isUserVisibleChunkState``;
        // pre-fix getChunk skipped that filter, so during the GC
        // window (state ∈ {gc_pending, purged} but the unlink hasn't
        // landed yet) HEAD 404'd while GET 200'd for the same id.
        // That race lets a chunk that the relay has just torn down
        // still serve content for a fraction of a second.
        if (
            $row === null
            || !self::isUserVisibleChunkState((string)$row['state'])
        ) {
            throw new VaultChunkMissingError($chunkId);
        }

        $absPath = VaultStorage::root() . '/' . (string)$row['storage_path'];
        if (!is_file($absPath)) {
            throw new VaultChunkMissingError($chunkId);
        }
        $bytes = file_get_contents($absPath);
        if ($bytes === false) {
            throw new VaultStorageUnavailableError("Failed to read chunk {$chunkId}");
        }

        Router::binary($bytes, 200);
    }

    // ===================================================================
    //  6.10  HEAD /api/vaults/{vault_id}/chunks/{chunk_id}
    // ===================================================================

    public static function headChunk(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $chunkId = (string)($ctx->params['chunk_id'] ?? '');
        if (!VaultChunksRepository::isValidChunkId($chunkId)) {
            throw new VaultInvalidRequestError('chunk_id format invalid', 'chunk_id');
        }
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $chunksRepo = new VaultChunksRepository($db);
        $head = $chunksRepo->head($vaultId, $chunkId);
        if ($head === null || !self::isUserVisibleChunkState((string)$head['state'])) {
            // F-S10: purged / gc_pending rows must not advertise as
            // present — the bytes are gone. 404 with no body.
            http_response_code(404);
            return;
        }

        http_response_code(200);
        header('Content-Length: ' . (int)$head['ciphertext_size']);
        header('X-Chunk-Hash: ' . (string)$head['chunk_hash']);
        header('X-Chunk-Stored-At: ' . self::ts((int)$head['created_at']));
    }

    /**
     * Validate a vault-migration target relay URL.
     *
     * Review §1.H3: ``migrationCommit`` checked scheme + filter_var
     * but ``migrationStart`` accepted any non-empty string. Both
     * endpoints persist the URL into the same table and both expose
     * it via GET /header, so the validation policy must be shared.
     * Throws ``VaultInvalidRequestError`` on any failure (caller
     * surfaces the standard 400 envelope).
     */
    private static function guardMigrationTargetRelayUrl(string $target): void
    {
        if (filter_var($target, FILTER_VALIDATE_URL) === false) {
            throw new VaultInvalidRequestError(
                'target_relay_url must be a valid URL',
                'target_relay_url',
            );
        }
        $scheme = parse_url($target, PHP_URL_SCHEME);
        if ($scheme === false || ($scheme !== 'http' && $scheme !== 'https')) {
            throw new VaultInvalidRequestError(
                'target_relay_url must use http(s)',
                'target_relay_url',
            );
        }
        if (!Config::migrationAllowPrivateUrls()) {
            $host = parse_url($target, PHP_URL_HOST);
            if (!is_string($host) || $host === '') {
                throw new VaultInvalidRequestError(
                    'target_relay_url must include a host',
                    'target_relay_url',
                );
            }
            self::rejectPrivateOrLoopbackHost($host);
        }
    }

    /**
     * Review §1.L2: refuse loopback / RFC 1918 private / link-local /
     * unique-local hosts so an admin can't push the paired fleet at an
     * internal service. The check covers both literal IPs (``127.0.0.1``,
     * ``192.168.x.x``, ``::1``, ``fc00::/7``) and DNS names that
     * unambiguously resolve to a loopback alias (``localhost``,
     * ``*.localhost``). Operators who legitimately need to migrate
     * across local URLs (dev rigs) opt in via
     * ``migrationAllowPrivateUrls: true`` in ``server/data/config.json``.
     */
    private static function rejectPrivateOrLoopbackHost(string $host): void
    {
        $normalized = strtolower(trim($host, "[]"));
        if ($normalized === 'localhost' || str_ends_with($normalized, '.localhost')) {
            throw new VaultInvalidRequestError(
                'target_relay_url points at a loopback host; set migrationAllowPrivateUrls=true to allow local URLs',
                'target_relay_url',
            );
        }
        if (filter_var(
            $normalized,
            FILTER_VALIDATE_IP,
            FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE,
        ) !== false) {
            // Public IP literal — allowed.
            return;
        }
        if (filter_var($normalized, FILTER_VALIDATE_IP) !== false) {
            // Literal IP that failed the public filter — private / reserved.
            throw new VaultInvalidRequestError(
                'target_relay_url points at a private / loopback / link-local IP; set migrationAllowPrivateUrls=true to allow local URLs',
                'target_relay_url',
            );
        }
        // DNS name that isn't a special-case loopback alias — accept.
        // We deliberately don't resolve here; DNS resolution at config
        // time is fragile (split horizons, TTL changes) and the
        // architectural answer to a malicious admin who controls DNS is
        // perimeter trust, not the relay's URL filter.
    }

    /** Whether a chunk's lifecycle state is visible to user-facing queries. */
    private static function isUserVisibleChunkState(string $state): bool
    {
        return $state === VaultChunksRepository::STATE_ACTIVE
            || $state === VaultChunksRepository::STATE_RETAINED;
    }

    /**
     * Review §1.C2: residual-unlink reaper. Walks rows in ``purged``
     * state, retries the file unlink (idempotent — a missing file is
     * treated as success), then conditionally deletes the row when the
     * blob is verifiably gone. ``deleteIfPurged`` keeps the delete
     * state-guarded so a concurrent §1.C1 revival can't be erased.
     *
     * Failures are logged and skipped — the orphan stays for the next
     * gc/execute to retry, so a sticky EBUSY/EIO doesn't break the GC
     * pass that's about to follow.
     */
    private static function reapResidualPurged(
        Database $db,
        string $vaultId,
        VaultChunksRepository $chunksRepo
    ): void {
        foreach ($chunksRepo->listPurged($vaultId) as $row) {
            $relativePath = (string)$row['storage_path'];
            $chunkId = (string)$row['chunk_id'];
            $absPath = VaultStorage::root() . '/' . $relativePath;
            $fileGone = !is_file($absPath);
            if (!$fileGone) {
                $fileGone = @unlink($absPath) === true;
                if (!$fileGone) {
                    AppLog::log('vault', sprintf(
                        'vault.gc.unlink_failed chunk=%s path=%s',
                        substr($chunkId, 0, 12),
                        $absPath,
                    ), 'warning');
                    continue;
                }
            }
            $chunksRepo->deleteIfPurged($vaultId, $chunkId);
        }
    }

    // ===================================================================
    //  §4.M1  GET /api/vaults/{vault_id}/chunks?cursor=…&limit=…
    // ===================================================================

    /**
     * §4.M1 — paginated list of user-visible chunk_ids for the vault.
     *
     * Desktop reaper enumerates server-side chunks, subtracts the set
     * referenced by the live manifest, and DELETEs the diff via the
     * existing admin-gated gc/execute path. Only ``active`` +
     * ``retained`` rows surface here (matches ``batchHead``'s filter
     * via ``isUserVisibleChunkState``).
     *
     * Auth shape mirrors ``batchHead``: vault-bearer required, no
     * device-role restriction beyond that — the data exposed is
     * already implicit in the manifest chain the client just
     * fetched.
     */
    public static function listChunks(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $cursorRaw = (string)($ctx->query['cursor'] ?? '');
        if ($cursorRaw !== '' && !VaultChunksRepository::isValidChunkId($cursorRaw)) {
            throw new VaultInvalidRequestError(
                'cursor must be a valid chunk_id or empty', 'cursor'
            );
        }
        $limitRaw = $ctx->query['limit'] ?? null;
        $limit = 1024;
        if ($limitRaw !== null) {
            if (!ctype_digit((string)$limitRaw)) {
                throw new VaultInvalidRequestError(
                    'limit must be a positive integer', 'limit'
                );
            }
            $limit = (int)$limitRaw;
            if ($limit < 1 || $limit > 1024) {
                throw new VaultInvalidRequestError(
                    'limit must be in [1, 1024]', 'limit'
                );
            }
        }
        // B2: ``min_age_seconds`` excludes recently-uploaded chunks
        // from the listing so the desktop reaper doesn't race with
        // a concurrent upload's between-PUT-and-publish window.
        $minAgeRaw = $ctx->query['min_age_seconds'] ?? null;
        $minAgeSeconds = 0;
        if ($minAgeRaw !== null) {
            if (!ctype_digit((string)$minAgeRaw)) {
                throw new VaultInvalidRequestError(
                    'min_age_seconds must be a non-negative integer',
                    'min_age_seconds'
                );
            }
            $minAgeSeconds = (int)$minAgeRaw;
            if ($minAgeSeconds < 0 || $minAgeSeconds > 86400 * 30) {
                throw new VaultInvalidRequestError(
                    'min_age_seconds must be in [0, 30 days]',
                    'min_age_seconds'
                );
            }
        }

        $chunksRepo = new VaultChunksRepository($db);
        $ids = $chunksRepo->listIds(
            $vaultId, $cursorRaw, $limit, $minAgeSeconds,
        );
        $nextCursor = (count($ids) === $limit) ? end($ids) : null;

        Router::json([
            'ok' => true,
            'data' => [
                'chunk_ids'   => $ids,
                'next_cursor' => $nextCursor,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.11  POST /api/vaults/{vault_id}/chunks/batch-head
    // ===================================================================

    public static function batchHead(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $body = $ctx->jsonBody();
        $ids = $body['chunk_ids'] ?? null;
        if (!is_array($ids)) {
            throw new VaultInvalidRequestError('chunk_ids must be an array', 'chunk_ids');
        }
        if (count($ids) > 1024) {
            throw new VaultInvalidRequestError('chunk_ids exceeds 1024 cap', 'chunk_ids');
        }
        foreach ($ids as $cid) {
            if (!is_string($cid) || !VaultChunksRepository::isValidChunkId($cid)) {
                throw new VaultInvalidRequestError(
                    "chunk_ids contains invalid id: " . (is_string($cid) ? $cid : '<non-string>'),
                    'chunk_ids'
                );
            }
        }

        $chunksRepo = new VaultChunksRepository($db);
        $rows = $chunksRepo->batchHead($vaultId, $ids);

        $chunks = [];
        foreach ($rows as $cid => $info) {
            if ($info === null || !self::isUserVisibleChunkState((string)$info['state'])) {
                $chunks[$cid] = ['present' => false];
            } else {
                $chunks[$cid] = [
                    'present' => true,
                    'size'    => (int)$info['ciphertext_size'],
                    'hash'    => (string)$info['chunk_hash'],
                ];
            }
        }

        Router::json([
            'ok' => true,
            'data' => ['chunks' => (object)$chunks],
        ], 200);
    }

    // ===================================================================
    //  6.12  POST /api/vaults/{vault_id}/gc/plan
    // ===================================================================

    public static function gcPlan(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        $callerDevice = (string)($ctx->deviceId ?? "");
        VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'sync');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $rootRevision = Validators::requireInt($body, 'root_revision');
        $candidateIds = $body['candidate_chunk_ids'] ?? null;

        // Review §3.C1: eviction stages 2/3 are hard-purges (unexpired
        // tombstones, oldest historical versions) — they must be gated
        // behind admin role, not the default sync role. Clients flag
        // such plans via ``purpose='forced_eviction'``; gcExecute then
        // enforces admin on the resulting job. ``purpose='sync'`` (the
        // default) keeps the historical sync_plan behaviour for the
        // safe stage-1 expired-tombstone path.
        $purpose = isset($body['purpose']) && is_string($body['purpose'])
            ? $body['purpose']
            : 'sync';
        if (!in_array($purpose, ['sync', 'forced_eviction'], true)) {
            throw new VaultInvalidRequestError(
                "purpose must be 'sync' or 'forced_eviction'",
                'purpose'
            );
        }
        if ($purpose === 'forced_eviction') {
            VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'admin');
        }
        if (!is_array($candidateIds)) {
            throw new VaultInvalidRequestError('candidate_chunk_ids must be array', 'candidate_chunk_ids');
        }
        foreach ($candidateIds as $cid) {
            if (!is_string($cid) || !VaultChunksRepository::isValidChunkId($cid)) {
                throw new VaultInvalidRequestError(
                    'candidate_chunk_ids contains invalid id',
                    'candidate_chunk_ids'
                );
            }
        }

        // Validate the root revision exists.
        $rootRepo = new VaultRootManifestsRepository($db);
        if ($rootRepo->getByRevision($vaultId, $rootRevision) === null) {
            throw new VaultInvalidRequestError(
                "root_revision {$rootRevision} unknown",
                'root_revision'
            );
        }

        // T1.6 minimal: trust the client's candidate list. Cross-check
        // against per-manifest reference indexes is T6/T7+ — for now any
        // chunk in vault_chunks (state=active) is "safe to delete" as far
        // as the relay can tell. No still_referenced entries returned.
        //
        // ``already_deleted_chunk_ids`` lists candidates that ``batchHead``
        // didn't find at all (the chunk row is gone). The client uses this
        // to clean stale shard entries after a partial-stage crash: a
        // prior eviction ran ``gc_execute`` (chunks deleted) but crashed
        // before publishing the shard mutations; the next run finds the
        // same expired-tombstone entries, asks ``gc_plan`` about their
        // chunks, gets back an empty ``safe_to_delete`` (chunks already
        // gone) plus a non-empty ``already_deleted_chunk_ids``, and runs
        // shard-cleanup-only without re-running ``gc_execute``.
        $chunksRepo = new VaultChunksRepository($db);
        $batch = $chunksRepo->batchHead($vaultId, $candidateIds);
        $safe = [];
        $alreadyDeleted = [];
        foreach ($batch as $cid => $info) {
            if ($info === null) {
                $alreadyDeleted[] = $cid;
            } elseif ($info['state'] === VaultChunksRepository::STATE_ACTIVE) {
                $safe[] = $cid;
            }
        }

        $jobId = self::generateId('pl');
        $now   = time();
        $expiresAt = $now + 900; // 15-minute plan TTL per vault-v1.md §6.12
        $deviceId  = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');

        $jobsRepo = new VaultGcJobsRepository($db);
        $jobKind = $purpose === 'forced_eviction'
            ? VaultGcJobsRepository::KIND_FORCED_EVICTION
            : VaultGcJobsRepository::KIND_SYNC_PLAN;
        $jobsRepo->create(
            $jobId,
            $vaultId,
            $jobKind,
            $safe,
            null,
            $expiresAt,
            $deviceId,
            $now
        );

        Router::json([
            'ok' => true,
            'data' => [
                'plan_id'                  => $jobId,
                'safe_to_delete'           => $safe,
                'still_referenced'         => [],
                'already_deleted_chunk_ids' => $alreadyDeleted,
                'expires_at'               => self::ts($expiresAt),
            ],
        ], 200);
    }

    // ===================================================================
    //  6.13  POST /api/vaults/{vault_id}/gc/execute
    // ===================================================================

    public static function gcExecute(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $planId = self::vaultRequireNonEmptyString($body, 'plan_id');

        // Review §1.M3 — minimum-role gate up front so a ``read-only``
        // caller cannot probe plan IDs and learn state from the
        // downstream error codes (completed→200, cancelled/expired→404,
        // planned→400). Below-sync roles get a uniform 403 before any
        // plan inspection runs. The per-kind admin re-check below
        // still escalates for SCHEDULED_PURGE / FORCED_EVICTION.
        $callerDevice = (string)($ctx->deviceId ?? "");
        VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'sync');

        $jobsRepo = new VaultGcJobsRepository($db);
        $job = $jobsRepo->getById($planId);
        if ($job === null || $job['vault_id'] !== $vaultId) {
            throw new VaultNotFoundError($planId);
        }
        // F-S01: spec §5/§6.13 — gc/execute is idempotent. Re-running on
        // a `completed` job returns the persisted totals; cancelled or
        // expired plans 404; other states are still hard errors.
        if ($job['state'] === VaultGcJobsRepository::STATE_COMPLETED) {
            $targetCount = is_array($job['target_chunk_ids'])
                ? count($job['target_chunk_ids'])
                : 0;
            $deleted = (int) ($job['deleted_count'] ?? 0);
            Router::json([
                'ok' => true,
                'data' => [
                    'plan_id'                => $planId,
                    'deleted_count'          => $deleted,
                    'skipped_count'          => max(0, $targetCount - $deleted),
                    'freed_ciphertext_bytes' => (int) ($job['freed_bytes'] ?? 0),
                ],
            ], 200);
            return;
        }
        if (
            $job['state'] === VaultGcJobsRepository::STATE_CANCELLED
            || $job['state'] === VaultGcJobsRepository::STATE_EXPIRED
        ) {
            throw new VaultNotFoundError($planId);
        }
        if ($job['state'] !== VaultGcJobsRepository::STATE_PLANNED) {
            throw new VaultInvalidRequestError(
                "plan is in state '{$job['state']}', not 'planned'",
                'plan_id'
            );
        }
        if ((int)$job['expires_at'] < time()) {
            throw new VaultNotFoundError($planId);
        }

        // §6.13: sync GC requires role=sync (already enforced upfront
        // by the §1.M3 gate above); scheduled_purge requires role=admin
        // AND a valid purge_secret (vault_purge_not_allowed covers both
        // the role gap and a wrong-secret). Review §3.C1:
        // forced_eviction (eviction stages 2/3) is a hard-purge — it
        // requires role=admin too, but does NOT yet enforce
        // purge_secret (the desktop's 507-quota flow has no passphrase
        // prompt yet; that's tracked as the spec-conformance follow-up
        // in docs/plans/unfinished.md §3.C1).
        if ($job['kind'] === VaultGcJobsRepository::KIND_SCHEDULED_PURGE) {
            try {
                VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'admin');
            } catch (VaultAccessDeniedError $e) {
                throw new VaultPurgeNotAllowedError(
                    'hard-purge requires role=admin'
                );
            }
            $purgeSecret = $body['purge_secret'] ?? null;
            if (!is_string($purgeSecret) || $purgeSecret === '') {
                throw new VaultPurgeNotAllowedError('purge_secret required for scheduled_purge');
            }
            if (!hash_equals(
                (string)$vault['purge_token_hash'],
                hash('sha256', $purgeSecret, true)
            )) {
                throw new VaultPurgeNotAllowedError('purge_secret does not match');
            }
        } elseif ($job['kind'] === VaultGcJobsRepository::KIND_FORCED_EVICTION) {
            try {
                VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'admin');
            } catch (VaultAccessDeniedError $e) {
                throw new VaultPurgeNotAllowedError(
                    'forced eviction requires role=admin'
                );
            }
        }
        // The default sync-only GC path needs no extra role check —
        // the upfront §1.M3 ``requireRole(sync)`` gate covered it.

        $chunksRepo = new VaultChunksRepository($db);
        $vaultsRepo = new VaultsRepository($db);

        // Review §1.C2: residual-unlink reaper. A previous gcExecute that
        // committed `state := purged` but crashed (or hit EBUSY/EIO)
        // before its post-commit unlink left an orphan blob on disk with
        // no scheduled retry — line 1206 below skips purged rows, so the
        // file would leak forever. Walk the purged set, retry the unlink,
        // and delete the row only when the file is verifiably gone. The
        // ``deleteIfPurged`` state guard defends against the §1.C1
        // revival race (a concurrent putChunk may have flipped the row
        // back to active in the meantime).
        self::reapResidualPurged($db, $vaultId, $chunksRepo);

        // F-S12: collect plans first, then commit DB state in a single
        // transaction, then unlink files. A crash mid-loop now leaves
        // either (a) no DB change + files intact, or (b) all DB changes
        // committed + some files possibly still on disk (those get
        // cleaned up at next gc/execute call which is idempotent now).
        $deletedCount = 0;
        $freedBytes   = 0;
        /** @var list<array{chunk_id: string, path: string}> $unlinkTargets */
        $unlinkTargets = [];
        $now = time();

        $db->execute('BEGIN IMMEDIATE');
        try {
            foreach ($job['target_chunk_ids'] as $cid) {
                $row = $chunksRepo->get($vaultId, $cid);
                if ($row === null) {
                    continue; // already gone
                }
                if ($row['state'] === VaultChunksRepository::STATE_PURGED) {
                    continue; // idempotent re-execute
                }
                $chunksRepo->setState($vaultId, $cid, VaultChunksRepository::STATE_PURGED);
                $deletedCount++;
                $freedBytes += (int)$row['ciphertext_size'];
                $unlinkTargets[] = [
                    'chunk_id' => (string)$cid,
                    'path'     => VaultStorage::root() . '/' . (string)$row['storage_path'],
                ];
            }
            if ($deletedCount > 0) {
                $vaultsRepo->incUsedBytes($vaultId, -$freedBytes, -$deletedCount, $now);
            }
            $jobsRepo->markCompleted($planId, $deletedCount, $freedBytes, $now);
            $db->execute('COMMIT');
        } catch (\Throwable $e) {
            try { $db->execute('ROLLBACK'); } catch (\Throwable $ignored) {}
            throw $e;
        }

        // After-commit unlinks. A failure here is logged; the bytes
        // count is already debited from the vault. Successful unlinks
        // also delete the now-orphan ``purged`` row so it doesn't keep
        // looping through the §1.C2 reaper on every subsequent
        // gc/execute — ``deleteIfPurged`` keeps the delete state-guarded
        // for the §1.C1 revival race. Persistent EBUSY/EIO leaves the
        // row in place; the next gc/execute's residual reaper retries.
        foreach ($unlinkTargets as $target) {
            $absPath = $target['path'];
            $chunkId = $target['chunk_id'];
            $fileGone = !is_file($absPath);
            if (!$fileGone) {
                $fileGone = @unlink($absPath) === true;
                if (!$fileGone) {
                    AppLog::log('vault', sprintf(
                        'vault.gc.unlink_failed plan=%s path=%s',
                        substr($planId, 0, 12),
                        $absPath,
                    ), 'warning');
                    continue;
                }
            }
            $chunksRepo->deleteIfPurged($vaultId, $chunkId);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'plan_id'                => $planId,
                'deleted_count'          => $deletedCount,
                'skipped_count'          => count($job['target_chunk_ids']) - $deletedCount,
                'freed_ciphertext_bytes' => $freedBytes,
            ],
        ], 200);
    }

    // ===================================================================
    //  6.14  POST /api/vaults/{vault_id}/gc/cancel
    // ===================================================================

    public static function gcCancel(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);

        $body = $ctx->jsonBody();
        $planId = isset($body['plan_id']) && is_string($body['plan_id']) && $body['plan_id'] !== ''
            ? $body['plan_id'] : null;
        $jobId  = isset($body['job_id']) && is_string($body['job_id']) && $body['job_id'] !== ''
            ? $body['job_id'] : null;
        if ($planId === null && $jobId === null) {
            throw new VaultInvalidRequestError('plan_id or job_id required', 'plan_id');
        }

        // §6.14: sync_plan jobs need role=sync; scheduled_purge jobs need
        // role=admin. Per-job lookup so a sync-role caller cancelling a
        // mix of sync_plan + scheduled_purge ids fails on the purge ids
        // rather than silently downgrading.
        $callerDevice = (string)($ctx->deviceId ?? "");
        $jobsRepo = new VaultGcJobsRepository($db);
        $now = time();
        foreach (array_filter([$planId, $jobId]) as $id) {
            $row = $jobsRepo->getById((string)$id);
            if ($row === null || $row['vault_id'] !== $vaultId) {
                // Idempotent: unknown / wrong-vault ids silently no-op so
                // toggle-OFF retries (§A17) don't error.
                continue;
            }
            if (
                $row['kind'] === VaultGcJobsRepository::KIND_SCHEDULED_PURGE
                || $row['kind'] === VaultGcJobsRepository::KIND_FORCED_EVICTION
            ) {
                try {
                    VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'admin');
                } catch (VaultAccessDeniedError $e) {
                    $label = $row['kind'] === VaultGcJobsRepository::KIND_SCHEDULED_PURGE
                        ? 'scheduled_purge' : 'forced_eviction';
                    throw new VaultPurgeNotAllowedError(
                        "cancelling a {$label} requires role=admin"
                    );
                }
            } else {
                VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'sync');
                // Review §1.H5: a sync-role caller may only cancel
                // jobs THEY requested (or be admin). Pre-fix any
                // sync-role device could cancel another admin's
                // open sync_plan, eventually exhausting the quota by
                // forcing repeated re-plans. The
                // ``requested_by_device_id`` column was already
                // recorded; this is the consult that was missing.
                $ownerDevice = (string)($row['requested_by_device_id'] ?? '');
                if (
                    $ownerDevice !== ''
                    && $ownerDevice !== $callerDevice
                ) {
                    try {
                        VaultAuthService::requireRole(
                            $db, $vaultId, $callerDevice, 'admin',
                        );
                    } catch (VaultAccessDeniedError $e) {
                        throw new VaultAccessDeniedError(
                            'cancelling another device\'s gc plan requires role=admin',
                            requiredRole: 'admin',
                        );
                    }
                }
            }
            $jobsRepo->markCancelled((string)$id, $now);
        }

        http_response_code(204);
    }

    // ===================================================================
    //  6.15  POST /api/vaults/{vault_id}/migration/start            (T9.2)
    // ===================================================================

    /**
     * Source-side relay records the user's intent to migrate this vault
     * to ``$body['target_relay_url']``. Returns a bearer token the
     * initiating device hands to the target relay so the target can
     * prove the source authorized this migration. Idempotent: a
     * second call with the same target returns the existing record;
     * a different target while an intent is already in flight 409s.
     */
    public static function migrationStart(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'admin');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $target = Validators::requireNonEmptyString($body, 'target_relay_url');
        // Review §1.H3: validate the URL at /migration/start too.
        // The pre-fix path accepted ``javascript:`` / ``data:`` /
        // ``file://`` / internal-only URLs, then re-emitted them via
        // /migration/verify-source to admin callers — the desktop's
        // switch-relay path might have followed the bad URL before
        // /migration/commit's check fired. Same policy now lives in
        // both endpoints.
        self::guardMigrationTargetRelayUrl($target);
        $deviceId = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');

        $intentsRepo = new VaultMigrationIntentsRepository($db);
        $now = time();

        // Review §1.C3: avoid generating + discarding a token on the
        // retry path. Inspect the intent first; only mint a token when
        // we're either creating a fresh row OR rotating for a recovery
        // retry from the same initiating device. A cross-target retry
        // still 409s; a different-device retry returns metadata-only
        // (token null) so it can't silently rotate someone else's
        // bearer.
        $existing = $intentsRepo->getIntent($vaultId);
        if ($existing !== null) {
            if ((string)$existing['target_relay_url'] !== $target) {
                throw new VaultMigrationInProgressError(
                    'started',
                    (string)$existing['target_relay_url'],
                );
            }
            if ((string)$existing['initiating_device'] === $deviceId) {
                // Same-device retry — rotate the stored hash and hand
                // back a fresh token so a dropped 201 from the original
                // /start doesn't permanently strand the migration. The
                // previously-issued token (if any) is invalidated.
                $token = self::generateMigrationToken();
                $tokenHashRaw = hash('sha256', $token, true);
                $intentsRepo->rotateTokenHash($vaultId, $tokenHashRaw);
                Router::json([
                    'ok' => true,
                    'data' => [
                        'vault_id'         => self::dashedVaultId($vaultId),
                        'target_relay_url' => (string)$existing['target_relay_url'],
                        'started_at'       => self::ts((int)$existing['started_at']),
                        'token'            => $token,
                        'token_returned'   => true,
                    ],
                ], 200);
                return;
            }
            // Different initiating device on retry: metadata-only so a
            // co-admin can observe but can't steal the bearer.
            Router::json([
                'ok' => true,
                'data' => [
                    'vault_id'         => self::dashedVaultId($vaultId),
                    'target_relay_url' => (string)$existing['target_relay_url'],
                    'started_at'       => self::ts((int)$existing['started_at']),
                    'token'            => null,
                    'token_returned'   => false,
                ],
            ], 200);
            return;
        }

        // Fresh intent.
        $token = self::generateMigrationToken();
        $tokenHashRaw = hash('sha256', $token, true);
        $result = $intentsRepo->recordIntent(
            $vaultId, $tokenHashRaw, $target, $deviceId, $now,
        );
        $record = $result['record'];

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'         => self::dashedVaultId($vaultId),
                'target_relay_url' => $target,
                'started_at'       => self::ts((int)$record['started_at']),
                'token'            => $token,
                'token_returned'   => true,
            ],
        ], 201);
    }

    // ===================================================================
    //  6.16  GET /api/vaults/{vault_id}/migration/verify-source     (T9.2)
    // ===================================================================

    /**
     * Source returns its authoritative manifest_hash + chunk_count +
     * used_ciphertext_bytes so the client can diff against the target
     * relay's vault row. Read-only: a vault that's already committed
     * (migrated_to set) is still readable for verification, so this
     * endpoint deliberately does NOT call ``guardReadOnly``.
     */
    public static function migrationVerifySource(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'admin');

        $intentsRepo = new VaultMigrationIntentsRepository($db);
        $intent = $intentsRepo->getIntent($vaultId);
        if ($intent === null) {
            throw new VaultInvalidRequestError(
                'no migration in progress for this vault',
                'vault_id',
            );
        }

        $rootHash = $vault['current_root_hash'] !== null
            ? (string)$vault['current_root_hash'] : '';
        $chunkCount = (int)($vault['chunk_count'] ?? 0);
        $usedBytes  = (int)($vault['used_ciphertext_bytes'] ?? 0);

        // F-S05: stamp verified_at idempotently. Subsequent verify-source
        // calls return the same timestamp.
        //
        // Review §1.M7 — explicit state guard so the semantics don't
        // rely on transitive COALESCE behaviour. A vault that already
        // committed (``migrated_to`` set) must NOT acquire a brand-new
        // ``verified_at`` after the commit point — verification is a
        // pre-commit primitive and the field should only ever record
        // when the pre-commit state was attested. Reading a verified_at
        // that was stamped post-commit would mislead the client about
        // the time-order of the migration phases. Pre-fix the behaviour
        // happened to be correct because COALESCE preserved any
        // pre-commit timestamp, but a vault with no prior verify that
        // then committed (e.g. via /commit's read-only auto-verify path
        // or any future code that skips verify) would still have
        // ``verified_at = NULL`` and the next /verify-source call would
        // stamp it AFTER the commit. The explicit guard makes the
        // invariant readable in one place.
        $alreadyCommitted = $vault['migrated_to'] !== null;
        if (!$alreadyCommitted) {
            $intentsRepo->markVerified($vaultId, time());
        }
        $persistedIntent = $intentsRepo->getIntent($vaultId);
        $verifiedAt = $persistedIntent !== null && $persistedIntent['verified_at'] !== null
            ? (int)$persistedIntent['verified_at']
            : time();

        // Per-shard hash map mirrors the root's encrypted
        // `remote_folders[*].shard_hash` so the migration verify path
        // can compare without re-decrypting the root.
        $shardHashes = [];
        $rows = $db->queryAll(
            'SELECT remote_folder_id, current_shard_hash
             FROM vault_folder_shard_heads
             WHERE vault_id = :id
             ORDER BY remote_folder_id ASC',
            [':id' => $vaultId],
        );
        foreach ($rows as $row) {
            $shardHashes[(string)$row['remote_folder_id']] = (string)$row['current_shard_hash'];
        }
        // Cast to object so json_encode emits `{}` instead of `[]` when
        // empty (clients consume this as a map keyed by remote_folder_id).
        $shardHashesJson = empty($shardHashes) ? (object)[] : $shardHashes;

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'                 => self::dashedVaultId($vaultId),
                'root_revision'            => (int)($vault['current_root_revision'] ?? 0),
                'root_hash'                => $rootHash,
                'shard_hashes'             => $shardHashesJson,
                'chunk_count'              => $chunkCount,
                'used_ciphertext_bytes'    => $usedBytes,
                'target_relay_url'         => (string)$intent['target_relay_url'],
                'started_at'               => self::ts((int)$intent['started_at']),
                'verified_at'              => self::ts($verifiedAt),
            ],
        ], 200);
    }

    // ===================================================================
    //  6.17  PUT /api/vaults/{vault_id}/migration/commit            (T9.2)
    // ===================================================================

    /**
     * Source flips the vault to read-only by stamping ``migrated_to``.
     * Idempotent: re-committing to the same target is a no-op; trying
     * to commit to a *different* target raises 409 (the original
     * target wins, matching ``/start``'s semantics).
     */
    public static function migrationCommit(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId, $ctx);
        VaultAuthService::requireRole($db, $vaultId, (string)($ctx->deviceId ?? ""), 'admin');

        $body = $ctx->jsonBody();
        $target = self::vaultRequireNonEmptyString($body, 'target_relay_url');
        // F-S14: target_relay_url is exposed to all paired devices via
        // GET /header. Keep the storage shape clean by validating to a
        // real http(s) URL before we commit it.
        self::guardMigrationTargetRelayUrl($target);

        $intentsRepo = new VaultMigrationIntentsRepository($db);
        $intent = $intentsRepo->getIntent($vaultId);
        if ($intent === null) {
            throw new VaultInvalidRequestError(
                'no migration in progress for this vault — call /migration/start first',
                'vault_id',
            );
        }
        if ((string)$intent['target_relay_url'] !== $target) {
            throw new VaultMigrationInProgressError(
                'started', (string)$intent['target_relay_url'],
            );
        }
        // F-S05: spec §7.3 — commit before verify is 409 with state="started".
        if ($intent['verified_at'] === null) {
            throw new VaultMigrationInProgressError(
                'started', (string)$intent['target_relay_url'],
            );
        }

        $now = time();
        $vaultsRepo = new VaultsRepository($db);
        $stamped = $vaultsRepo->markMigratedTo($vaultId, $target, $now);
        if (!$stamped) {
            // Already migrated_to a different URL — surface conflict.
            $current = $vaultsRepo->getById($vaultId);
            $existing = $current !== null && $current['migrated_to'] !== null
                ? (string)$current['migrated_to'] : '';
            throw new VaultMigrationInProgressError('committed', $existing);
        }
        $intentsRepo->markCommitted($vaultId, $now);

        // Re-read so the response reflects whatever timestamp was
        // actually persisted (idempotent retry returns the original).
        $persistedIntent = $intentsRepo->getIntent($vaultId);
        $committedAt = $persistedIntent !== null && $persistedIntent['committed_at'] !== null
            ? (int)$persistedIntent['committed_at']
            : $now;

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'         => self::dashedVaultId($vaultId),
                'target_relay_url' => $target,
                'committed_at'     => self::ts($committedAt),
            ],
        ], 200);
    }

    // ===================================================================
    //  helpers
    // ===================================================================

    /**
     * Generate the migration bearer token returned by /migration/start.
     * 30 lowercase base32 chars (150 bits) — same alphabet as the rest
     * of the vault id-space so the token survives any URL-safe path.
     */
    private static function generateMigrationToken(): string
    {
        $alphabet = 'abcdefghijklmnopqrstuvwxyz234567';
        $rand = random_bytes(19); // 19 * 8 = 152 bits → 30 base32 chars
        $out = '';
        $bits = 0;
        $buf = 0;
        for ($i = 0; $i < 19; $i++) {
            $buf = ($buf << 8) | ord($rand[$i]);
            $bits += 8;
            while ($bits >= 5) {
                $bits -= 5;
                $out .= $alphabet[($buf >> $bits) & 0x1f];
            }
        }
        return 'mig_v1_' . substr($out, 0, 30);
    }

    /**
     * Reject writes when the vault is read-only on this relay (post-H2 commit
     * or soft-deleted at the relay). Read endpoints don't call this; only the
     * write-path (PUT/POST that mutates state).
     */
    private static function guardReadOnly(array $vault): void
    {
        if ($vault['migrated_to'] !== null) {
            throw new VaultMigrationInProgressError('committed', (string)$vault['migrated_to']);
        }
        if ($vault['soft_deleted_at'] !== null) {
            throw new VaultMigrationInProgressError('soft_deleted');
        }
    }

    /**
     * `<prefix>_v1_<24 base32 lowercase>` random id, matching the formats §3.3
     * convention. CSPRNG-backed via PHP 7.0+ `random_bytes`.
     */
    private static function generateId(string $prefix): string
    {
        $alphabet = 'abcdefghijklmnopqrstuvwxyz234567'; // RFC 4648 base32 lowercase
        $rand = random_bytes(15);                       // 15*8 = 120 bits → 24 base32 chars
        $out = '';
        $bits = 0;
        $buf = 0;
        for ($i = 0; $i < 15; $i++) {
            $buf = ($buf << 8) | ord($rand[$i]);
            $bits += 8;
            while ($bits >= 5) {
                $bits -= 5;
                $out .= $alphabet[($buf >> $bits) & 0x1f];
            }
        }
        return $prefix . '_v1_' . $out;
    }

    /** Spec §3.3 grant id: `gr_v1_<24base32>`. */
    private static function generateGrantId(): string
    {
        return self::generateId('gr');
    }
}
