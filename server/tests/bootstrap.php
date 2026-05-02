<?php

/**
 * PHPUnit bootstrap. The relay has no Composer autoloader on the runtime
 * path (production loads classes via require_once chain in public/index.php),
 * so this file mirrors that chain for the test process.
 *
 * Vault tests in tests/Vault/ rely on Database + repositories under test
 * being loaded here; controller / service tests added later extend this.
 */

$root = dirname(__DIR__);

require_once $root . '/src/Database.php';
require_once $root . '/src/Config.php';
require_once $root . '/src/AppLog.php';

// Http pipeline (errors first — auth services raise from these).
require_once $root . '/src/Http/RequestContext.php';
require_once $root . '/src/Http/ApiError.php';
require_once $root . '/src/Http/VaultApiError.php';
require_once $root . '/src/Http/ErrorResponder.php';

// Auth.
require_once $root . '/src/Auth/AuthIdentity.php';
require_once $root . '/src/Auth/AuthService.php';
require_once $root . '/src/Auth/VaultAuthService.php';

// Repositories.
require_once $root . '/src/Repositories/DeviceRepository.php';
require_once $root . '/src/Repositories/VaultsRepository.php';
require_once $root . '/src/Repositories/VaultManifestsRepository.php';
require_once $root . '/src/Repositories/VaultChunksRepository.php';
