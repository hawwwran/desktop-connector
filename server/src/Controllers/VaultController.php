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
     * trying to OOM the relay. Manifest envelopes are 16 MiB.
     */
    public const MAX_CHUNK_BYTES    = 9 * 1024 * 1024;
    public const MAX_MANIFEST_BYTES = 16 * 1024 * 1024;
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

    /** base64-decode with strict mode + length check; throws on either failure. */
    private static function decodeBase64Field(array $body, string $field, ?int $expectedLength = null): string
    {
        $b64 = Validators::requireNonEmptyString($body, $field);
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
        $headerHash = self::vaultRequireNonEmptyString($body, 'header_hash');

        $manifestCipher = self::decodeBase64Field($body, 'initial_manifest_ciphertext');
        self::guardEnvelopeSize('manifest', strlen($manifestCipher), self::MAX_MANIFEST_BYTES);
        $manifestHash = self::vaultRequireNonEmptyString($body, 'initial_manifest_hash');

        // F-S11: persist the recovery-derived purge_token_hash so the
        // T14 hard-purge path can authenticate `purge_secret`. Optional
        // for older clients that haven't been migrated yet.
        $purgeTokenHash = null;
        if (isset($body['purge_token_hash'])) {
            $purgeTokenHash = self::decodeBase64Field($body, 'purge_token_hash', 32);
        }

        // T9.3 — relay-to-relay migration bootstraps the target at the
        // source's revision so manifest envelope AAD (which carries
        // revision/parent_revision) round-trips verbatim. Defaults to 1
        // for the standard create path.
        $initialManifestRevision = isset($body['initial_manifest_revision'])
            ? self::vaultRequireInt($body, 'initial_manifest_revision') : 1;
        $initialHeaderRevision = isset($body['initial_header_revision'])
            ? self::vaultRequireInt($body, 'initial_header_revision') : 1;
        if ($initialManifestRevision < 1 || $initialHeaderRevision < 1) {
            throw new VaultInvalidRequestError(
                'initial_*_revision must be >= 1',
                'initial_manifest_revision'
            );
        }

        $vaultsRepo = new VaultsRepository($db);
        if ($vaultsRepo->getById($vaultId) !== null) {
            throw new VaultAlreadyExistsError($vaultId);
        }

        $now = time();
        $vaultsRepo->create(
            $vaultId, $tokenHash, $encHeader, $headerHash, $manifestHash, $now,
            $initialHeaderRevision, $initialManifestRevision, $purgeTokenHash,
        );

        $manifestsRepo = new VaultManifestsRepository($db);
        $manifestsRepo->create(
            $vaultId,
            $initialManifestRevision,
            $initialManifestRevision - 1,
            $manifestHash,
            $manifestCipher,
            strlen($manifestCipher),
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
                'manifest_revision'      => $initialManifestRevision,
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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);

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
            ],
        ], 200);
    }

    // ===================================================================
    //  6.3  PUT /api/vaults/{vault_id}/header (CAS)
    // ===================================================================

    public static function putHeader(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'admin');
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
        $headerHash = self::vaultRequireNonEmptyString($body, 'header_hash');
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
    //  6.4  GET /api/vaults/{vault_id}/manifest
    // ===================================================================

    public static function getManifest(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);

        $manifestsRepo = new VaultManifestsRepository($db);
        $current = $manifestsRepo->getCurrent($vaultId);
        if ($current === null) {
            // A vault that exists but has no manifest is malformed; surface
            // as not-found rather than 500.
            throw new VaultNotFoundError($vaultId);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'revision'            => (int)$current['revision'],
                'parent_revision'     => (int)$current['parent_revision'],
                'manifest_hash'       => (string)$current['manifest_hash'],
                'manifest_ciphertext' => base64_encode((string)$current['manifest_ciphertext']),
                'manifest_size'       => (int)$current['manifest_size'],
            ],
        ], 200);
    }

    // ===================================================================
    //  6.5  GET /api/vaults/{vault_id}/manifest/revisions/{revision}
    // ===================================================================

    public static function getManifestRevision(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $revisionRaw = (string) ($ctx->params['revision'] ?? '');
        if ($revisionRaw === '' || !ctype_digit($revisionRaw)) {
            throw new VaultInvalidRequestError(
                'revision must be a non-negative integer', 'revision',
            );
        }
        $revision = (int) $revisionRaw;
        VaultAuthService::requireVaultAuth($db, $vaultId);

        $manifestsRepo = new VaultManifestsRepository($db);
        $row = $manifestsRepo->getByRevision($vaultId, $revision);
        if ($row === null) {
            throw new VaultNotFoundError("manifest revision {$revision} for {$vaultId}");
        }
        Router::json([
            'ok' => true,
            'data' => [
                'revision'            => (int) $row['revision'],
                'parent_revision'     => (int) $row['parent_revision'],
                'manifest_hash'       => (string) $row['manifest_hash'],
                'manifest_ciphertext' => base64_encode((string) $row['manifest_ciphertext']),
                'manifest_size'       => (int) $row['manifest_size'],
            ],
        ], 200);
    }

    // ===================================================================
    //  6.6  PUT /api/vaults/{vault_id}/manifest (CAS, A1 conflict)
    // ===================================================================

    public static function putManifest(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'browse-upload');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        // F-S04: vault routes must use vault-shaped errors. Wrap the
        // legacy validators so any "missing field" emits the vault_v1
        // envelope, not the legacy {"error":"…"} shape.
        $expected     = self::vaultRequireInt($body, 'expected_current_revision');
        $newRev       = self::vaultRequireInt($body, 'new_revision');
        $parentRev    = self::vaultRequireInt($body, 'parent_revision');
        $manifestHash = self::vaultRequireNonEmptyString($body, 'manifest_hash');
        $manifestCipher = self::decodeBase64Field($body, 'manifest_ciphertext');
        self::guardEnvelopeSize('manifest', strlen($manifestCipher), self::MAX_MANIFEST_BYTES);

        if ($parentRev !== $expected) {
            throw new VaultInvalidRequestError(
                'parent_revision must equal expected_current_revision',
                'parent_revision'
            );
        }
        if ($newRev !== $expected + 1) {
            throw new VaultInvalidRequestError(
                'new_revision must be expected_current_revision + 1',
                'new_revision'
            );
        }

        $manifestsRepo = new VaultManifestsRepository($db);
        $authorDeviceId = $ctx->deviceId ?? '';
        // requireVaultAuth doesn't populate $ctx->deviceId (Router only does
        // that for routes flagged requiresAuth=true). Recover via _SERVER.
        if ($authorDeviceId === '') {
            $authorDeviceId = VaultAuthService::callerDeviceId();
        }

        // Authoritatively read (vault_id, revision, parent_revision,
        // author_device_id) from the envelope's deterministic 61-byte
        // prefix (formats §10.1). The relay decides CAS off these bytes
        // rather than the JSON body, turning a class of envelope/body
        // drift bugs into 400s instead of a poisoned manifest chain.
        try {
            $envManifest = VaultCrypto::parseManifestEnvelopeHeader($manifestCipher);
        } catch (InvalidArgumentException $e) {
            throw new VaultInvalidRequestError($e->getMessage(), 'manifest_ciphertext');
        }
        // F-S09: reject unsupported format versions before the body is
        // committed. A v2 envelope cannot be stored on a v1 server.
        self::guardFormatVersion('manifest', (int) $envManifest['format_version']);
        if ($envManifest['vault_id'] !== $vaultId) {
            throw new VaultManifestTamperedError(
                'manifest envelope vault_id does not match path vault_id',
            );
        }
        if ($envManifest['revision'] !== $newRev) {
            throw new VaultManifestTamperedError(
                'manifest envelope revision does not match new_revision',
            );
        }
        if ($envManifest['parent_revision'] !== $parentRev) {
            throw new VaultManifestTamperedError(
                'manifest envelope parent_revision does not match body parent_revision',
            );
        }
        if ($authorDeviceId !== '' && $envManifest['author_device_id'] !== $authorDeviceId) {
            throw new VaultManifestTamperedError(
                'manifest envelope author_device_id does not match X-Device-ID',
            );
        }

        $now = time();
        $conflict = $manifestsRepo->tryCAS(
            $vaultId,
            $expected,
            $newRev,
            $manifestHash,
            $manifestCipher,
            strlen($manifestCipher),
            $authorDeviceId,
            $now
        );
        if ($conflict !== null) {
            throw new VaultManifestConflictError([
                'current_revision'             => (int)$conflict['current_revision'],
                'expected_revision'            => $expected,
                'current_manifest_hash'        => (string)$conflict['current_manifest_hash'],
                'current_manifest_ciphertext'  => base64_encode((string)$conflict['current_manifest_ciphertext']),
                'current_manifest_size'        => (int)$conflict['current_manifest_size'],
            ]);
        }

        Router::json([
            'ok' => true,
            'data' => [
                'revision'      => $newRev,
                'manifest_hash' => $manifestHash,
            ],
        ], 200);
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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'browse-upload');
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
            if ($chunksRepo->head($vaultId, $chunkId) === null) {
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
                $chunksRepo->deleteRow($vaultId, $chunkId);
                $vaultsRepo->incUsedBytes($vaultId, -$size, -1, $now);
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
        VaultAuthService::requireVaultAuth($db, $vaultId);

        $chunksRepo = new VaultChunksRepository($db);
        $row = $chunksRepo->get($vaultId, $chunkId);
        if ($row === null) {
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
        VaultAuthService::requireVaultAuth($db, $vaultId);

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

    /** Whether a chunk's lifecycle state is visible to user-facing queries. */
    private static function isUserVisibleChunkState(string $state): bool
    {
        return $state === VaultChunksRepository::STATE_ACTIVE
            || $state === VaultChunksRepository::STATE_RETAINED;
    }

    // ===================================================================
    //  6.11  POST /api/vaults/{vault_id}/chunks/batch-head
    // ===================================================================

    public static function batchHead(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        VaultAuthService::requireVaultAuth($db, $vaultId);

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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'sync');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $manifestRevision = Validators::requireInt($body, 'manifest_revision');
        $candidateIds = $body['candidate_chunk_ids'] ?? null;
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

        // Validate the manifest revision exists.
        $manifestsRepo = new VaultManifestsRepository($db);
        if ($manifestsRepo->getByRevision($vaultId, $manifestRevision) === null) {
            throw new VaultInvalidRequestError(
                "manifest_revision {$manifestRevision} unknown",
                'manifest_revision'
            );
        }

        // T1.6 minimal: trust the client's candidate list. Cross-check
        // against per-manifest reference indexes is T6/T7+ — for now any
        // chunk in vault_chunks (state=active) is "safe to delete" as far
        // as the relay can tell. No still_referenced entries returned.
        $chunksRepo = new VaultChunksRepository($db);
        $batch = $chunksRepo->batchHead($vaultId, $candidateIds);
        $safe = [];
        foreach ($batch as $cid => $info) {
            if ($info !== null && $info['state'] === VaultChunksRepository::STATE_ACTIVE) {
                $safe[] = $cid;
            }
        }

        $jobId = self::generateId('pl');
        $now   = time();
        $expiresAt = $now + 900; // 15-minute plan TTL per vault-v1.md §6.12
        $deviceId  = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');

        $jobsRepo = new VaultGcJobsRepository($db);
        $jobsRepo->create(
            $jobId,
            $vaultId,
            VaultGcJobsRepository::KIND_SYNC_PLAN,
            $safe,
            null,
            $expiresAt,
            $deviceId,
            $now
        );

        Router::json([
            'ok' => true,
            'data' => [
                'plan_id'          => $jobId,
                'safe_to_delete'   => $safe,
                'still_referenced' => [],
                'expires_at'       => self::ts($expiresAt),
            ],
        ], 200);
    }

    // ===================================================================
    //  6.13  POST /api/vaults/{vault_id}/gc/execute
    // ===================================================================

    public static function gcExecute(Database $db, RequestContext $ctx): void
    {
        $vaultId = self::normalizeVaultId($ctx->params['vault_id'] ?? '');
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $planId = self::vaultRequireNonEmptyString($body, 'plan_id');

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

        // §6.13: sync GC requires role=sync; scheduled_purge requires
        // role=admin AND a valid purge_secret (vault_purge_not_allowed
        // covers both the role gap and a wrong-secret).
        $callerDevice = VaultAuthService::callerDeviceId();
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
        } else {
            VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'sync');
        }

        $chunksRepo = new VaultChunksRepository($db);
        $vaultsRepo = new VaultsRepository($db);

        // F-S12: collect plans first, then commit DB state in a single
        // transaction, then unlink files. A crash mid-loop now leaves
        // either (a) no DB change + files intact, or (b) all DB changes
        // committed + some files possibly still on disk (those get
        // cleaned up at next gc/execute call which is idempotent now).
        $deletedCount = 0;
        $freedBytes   = 0;
        $unlinkPaths  = [];
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
                $unlinkPaths[] = VaultStorage::root() . '/' . (string)$row['storage_path'];
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
        // count is already debited from the vault, so leftover files
        // are dead-weight that the next purge will clean up.
        foreach ($unlinkPaths as $absPath) {
            if (is_file($absPath) && @unlink($absPath) === false) {
                AppLog::log('vault', sprintf(
                    'vault.gc.unlink_failed plan=%s path=%s',
                    substr($planId, 0, 12),
                    $absPath,
                ), 'warning');
            }
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
        VaultAuthService::requireVaultAuth($db, $vaultId);

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
        $callerDevice = VaultAuthService::callerDeviceId();
        $jobsRepo = new VaultGcJobsRepository($db);
        $now = time();
        foreach (array_filter([$planId, $jobId]) as $id) {
            $row = $jobsRepo->getById((string)$id);
            if ($row === null || $row['vault_id'] !== $vaultId) {
                // Idempotent: unknown / wrong-vault ids silently no-op so
                // toggle-OFF retries (§A17) don't error.
                continue;
            }
            if ($row['kind'] === VaultGcJobsRepository::KIND_SCHEDULED_PURGE) {
                try {
                    VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'admin');
                } catch (VaultAccessDeniedError $e) {
                    throw new VaultPurgeNotAllowedError(
                        'cancelling a scheduled_purge requires role=admin'
                    );
                }
            } else {
                VaultAuthService::requireRole($db, $vaultId, $callerDevice, 'sync');
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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'admin');
        self::guardReadOnly($vault);

        $body = $ctx->jsonBody();
        $target = Validators::requireNonEmptyString($body, 'target_relay_url');
        $deviceId = (string)($_SERVER['HTTP_X_DEVICE_ID'] ?? '');

        $intentsRepo = new VaultMigrationIntentsRepository($db);
        $now = time();

        // Generate a fresh token; if the intent already exists we keep
        // the *existing* token rather than rotating, which is what makes
        // the endpoint idempotent for retried POST /start calls.
        $token = self::generateMigrationToken();
        $tokenHashRaw = hash('sha256', $token, true);

        $result = $intentsRepo->recordIntent(
            $vaultId, $tokenHashRaw, $target, $deviceId, $now,
        );
        $record = $result['record'];

        if (!$result['created']) {
            // Pre-existing intent. Reject if the caller asked for a
            // different target — §H2 says a vault can only migrate to
            // one place at a time. Reuse-with-same-target returns
            // metadata only; the original token is *not* re-leaked
            // (the caller already received it on the first /start).
            if ((string)$record['target_relay_url'] !== $target) {
                throw new VaultMigrationInProgressError(
                    'started',
                    (string)$record['target_relay_url'],
                );
            }
            Router::json([
                'ok' => true,
                'data' => [
                    'vault_id'         => self::dashedVaultId($vaultId),
                    'target_relay_url' => (string)$record['target_relay_url'],
                    'started_at'       => self::ts((int)$record['started_at']),
                    'token'            => null,         // not re-emitted; idempotent
                    'token_returned'   => false,
                ],
            ], 200);
            return;
        }

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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'admin');

        $intentsRepo = new VaultMigrationIntentsRepository($db);
        $intent = $intentsRepo->getIntent($vaultId);
        if ($intent === null) {
            throw new VaultInvalidRequestError(
                'no migration in progress for this vault',
                'vault_id',
            );
        }

        $manifestHash = $vault['current_manifest_hash'] !== null
            ? (string)$vault['current_manifest_hash'] : '';
        $chunkCount = (int)($vault['chunk_count'] ?? 0);
        $usedBytes  = (int)($vault['used_ciphertext_bytes'] ?? 0);

        // F-S05: stamp verified_at idempotently. Subsequent verify-source
        // calls return the same timestamp.
        $intentsRepo->markVerified($vaultId, time());
        $persistedIntent = $intentsRepo->getIntent($vaultId);
        $verifiedAt = $persistedIntent !== null && $persistedIntent['verified_at'] !== null
            ? (int)$persistedIntent['verified_at']
            : time();

        Router::json([
            'ok' => true,
            'data' => [
                'vault_id'                 => self::dashedVaultId($vaultId),
                'manifest_revision'        => (int)($vault['current_manifest_revision'] ?? 0),
                'manifest_hash'            => $manifestHash,
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
        $vault = VaultAuthService::requireVaultAuth($db, $vaultId);
        VaultAuthService::requireRole($db, $vaultId, VaultAuthService::callerDeviceId(), 'admin');

        $body = $ctx->jsonBody();
        $target = self::vaultRequireNonEmptyString($body, 'target_relay_url');
        // F-S14: target_relay_url is exposed to all paired devices via
        // GET /header. Keep the storage shape clean by validating to a
        // real http(s) URL before we commit it.
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
