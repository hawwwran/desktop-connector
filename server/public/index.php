<?php

// Desktop Connector - PHP Relay Server

// Let the PHP built-in dev server pass through static files in this directory
// (favicons etc). Apache/.htaccess handles the same thing in production.
if (PHP_SAPI === 'cli-server') {
    $staticPath = __DIR__ . parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
    if (is_file($staticPath)) {
        return false;
    }
}

require_once __DIR__ . '/../src/Database.php';
require_once __DIR__ . '/../src/Config.php';

// --- Http pipeline ---
require_once __DIR__ . '/../src/Http/RequestContext.php';
require_once __DIR__ . '/../src/Http/ApiError.php';
require_once __DIR__ . '/../src/Http/VaultApiError.php';
require_once __DIR__ . '/../src/Http/ErrorResponder.php';
require_once __DIR__ . '/../src/Http/Validators.php';

// --- Auth ---
require_once __DIR__ . '/../src/Auth/AuthIdentity.php';
require_once __DIR__ . '/../src/Auth/AuthService.php';
require_once __DIR__ . '/../src/Auth/VaultAuthService.php';

// --- Repositories ---
require_once __DIR__ . '/../src/Repositories/DeviceRepository.php';
require_once __DIR__ . '/../src/Repositories/PairingRepository.php';
require_once __DIR__ . '/../src/Repositories/TransferRepository.php';
require_once __DIR__ . '/../src/Repositories/ChunkRepository.php';
require_once __DIR__ . '/../src/Repositories/FasttrackRepository.php';
require_once __DIR__ . '/../src/Repositories/PingRateRepository.php';
require_once __DIR__ . '/../src/Repositories/VaultsRepository.php';
require_once __DIR__ . '/../src/Repositories/VaultManifestsRepository.php';
require_once __DIR__ . '/../src/Repositories/VaultChunksRepository.php';
require_once __DIR__ . '/../src/Repositories/VaultGcJobsRepository.php';
require_once __DIR__ . '/../src/Repositories/VaultMigrationIntentsRepository.php';

require_once __DIR__ . '/../src/VaultStorage.php';
require_once __DIR__ . '/../src/VaultCapabilities.php';
require_once __DIR__ . '/../src/Router.php';

// --- Controllers ---
require_once __DIR__ . '/../src/Controllers/DeviceController.php';
require_once __DIR__ . '/../src/Controllers/PairingController.php';
require_once __DIR__ . '/../src/Controllers/TransferController.php';
require_once __DIR__ . '/../src/Controllers/DashboardController.php';
require_once __DIR__ . '/../src/Controllers/FcmController.php';
require_once __DIR__ . '/../src/Controllers/FasttrackController.php';
require_once __DIR__ . '/../src/Controllers/VaultController.php';

require_once __DIR__ . '/../src/FcmSender.php';
require_once __DIR__ . '/../src/AppLog.php';

// --- Domain (transfer lifecycle model) ---
require_once __DIR__ . '/../src/Domain/Transfer/TransferState.php';
require_once __DIR__ . '/../src/Domain/Transfer/TransferInvariants.php';
require_once __DIR__ . '/../src/Domain/Transfer/TransferLifecycle.php';
require_once __DIR__ . '/../src/Domain/Transfer/TransferStatusMapper.php';

// --- Services ---
require_once __DIR__ . '/../src/Services/TransferStatusService.php';
require_once __DIR__ . '/../src/Services/TransferNotifyService.php';
require_once __DIR__ . '/../src/Services/TransferWakeService.php';
require_once __DIR__ . '/../src/Services/TransferCleanupService.php';
require_once __DIR__ . '/../src/Services/TransferService.php';

// --- Messaging ---
require_once __DIR__ . '/../src/Messaging/MessageTransportPolicy.php';

// Initialize database
$db = Database::getInstance();
$db->migrate();

$router = new Router($db);

// --- Public routes (no auth) ---

$router->get('/api/health', function (RequestContext $ctx) use ($db) {
    DeviceController::health($db, $ctx);
});

$router->post('/api/devices/register', function (RequestContext $ctx) use ($db) {
    DeviceController::register($db, $ctx);
});

$router->get('/api/fcm/config', function (RequestContext $ctx) use ($db) {
    FcmController::config($db, $ctx);
});

$router->get('/dashboard', function (RequestContext $ctx) use ($db) {
    DashboardController::show($db);
});

// Redirect root to dashboard
$router->get('/', function (RequestContext $ctx) {
    header('Location: dashboard');
    exit;
});

// --- Authenticated routes ---

$router->authGet('/api/devices/stats', function (RequestContext $ctx) use ($db) {
    DeviceController::stats($db, $ctx);
});

$router->authPost('/api/devices/fcm-token', function (RequestContext $ctx) use ($db) {
    DeviceController::updateFcmToken($db, $ctx);
});

$router->authPost('/api/devices/ping', function (RequestContext $ctx) use ($db) {
    DeviceController::ping($db, $ctx);
});

$router->authPost('/api/devices/pong', function (RequestContext $ctx) use ($db) {
    DeviceController::pong($db, $ctx);
});

$router->authPost('/api/pairing/request', function (RequestContext $ctx) use ($db) {
    PairingController::request($db, $ctx);
});

$router->authGet('/api/pairing/poll', function (RequestContext $ctx) use ($db) {
    PairingController::poll($db, $ctx);
});

$router->authPost('/api/pairing/confirm', function (RequestContext $ctx) use ($db) {
    PairingController::confirm($db, $ctx);
});

$router->authPost('/api/transfers/init', function (RequestContext $ctx) use ($db) {
    TransferController::init($db, $ctx);
});

$router->authPost('/api/transfers/{transfer_id}/chunks/{chunk_index}', function (RequestContext $ctx) use ($db) {
    TransferController::uploadChunk($db, $ctx);
});

$router->authGet('/api/transfers/pending', function (RequestContext $ctx) use ($db) {
    TransferController::pending($db, $ctx);
});

$router->authGet('/api/transfers/{transfer_id}/chunks/{chunk_index}', function (RequestContext $ctx) use ($db) {
    TransferController::downloadChunk($db, $ctx);
});

$router->authPost('/api/transfers/{transfer_id}/ack', function (RequestContext $ctx) use ($db) {
    TransferController::ack($db, $ctx);
});

$router->authPost('/api/transfers/{transfer_id}/chunks/{chunk_index}/ack', function (RequestContext $ctx) use ($db) {
    TransferController::ackChunk($db, $ctx);
});

$router->authDelete('/api/transfers/{transfer_id}', function (RequestContext $ctx) use ($db) {
    TransferController::cancel($db, $ctx);
});

$router->authGet('/api/transfers/sent-status', function (RequestContext $ctx) use ($db) {
    TransferController::sentStatus($db, $ctx);
});

$router->authGet('/api/transfers/notify', function (RequestContext $ctx) use ($db) {
    TransferController::notify($db, $ctx);
});

// --- Fasttrack: lightweight encrypted message relay ---

$router->authPost('/api/fasttrack/send', function (RequestContext $ctx) use ($db) {
    FasttrackController::send($db, $ctx);
});

$router->authGet('/api/fasttrack/pending', function (RequestContext $ctx) use ($db) {
    FasttrackController::pending($db, $ctx);
});

$router->authPost('/api/fasttrack/{id}/ack', function (RequestContext $ctx) use ($db) {
    FasttrackController::ack($db, $ctx);
});

// --- Vault routes (vault_v1) ---
// All vault routes use vault*() helpers because their auth produces the
// vault_v1 error envelope; controllers call VaultAuthService themselves.
$router->vaultPost('/api/vaults', function (RequestContext $ctx) use ($db) {
    VaultController::create($db, $ctx);
});
$router->vaultGet('/api/vaults/{vault_id}/header', function (RequestContext $ctx) use ($db) {
    VaultController::getHeader($db, $ctx);
});
$router->vaultPut('/api/vaults/{vault_id}/header', function (RequestContext $ctx) use ($db) {
    VaultController::putHeader($db, $ctx);
});
$router->vaultGet('/api/vaults/{vault_id}/manifest', function (RequestContext $ctx) use ($db) {
    VaultController::getManifest($db, $ctx);
});
$router->vaultPut('/api/vaults/{vault_id}/manifest', function (RequestContext $ctx) use ($db) {
    VaultController::putManifest($db, $ctx);
});
$router->vaultPut('/api/vaults/{vault_id}/chunks/{chunk_id}', function (RequestContext $ctx) use ($db) {
    VaultController::putChunk($db, $ctx);
});
$router->vaultGet('/api/vaults/{vault_id}/chunks/{chunk_id}', function (RequestContext $ctx) use ($db) {
    VaultController::getChunk($db, $ctx);
});
$router->vaultHead('/api/vaults/{vault_id}/chunks/{chunk_id}', function (RequestContext $ctx) use ($db) {
    VaultController::headChunk($db, $ctx);
});
$router->vaultPost('/api/vaults/{vault_id}/chunks/batch-head', function (RequestContext $ctx) use ($db) {
    VaultController::batchHead($db, $ctx);
});
$router->vaultPost('/api/vaults/{vault_id}/gc/plan', function (RequestContext $ctx) use ($db) {
    VaultController::gcPlan($db, $ctx);
});
$router->vaultPost('/api/vaults/{vault_id}/gc/execute', function (RequestContext $ctx) use ($db) {
    VaultController::gcExecute($db, $ctx);
});
$router->vaultPost('/api/vaults/{vault_id}/gc/cancel', function (RequestContext $ctx) use ($db) {
    VaultController::gcCancel($db, $ctx);
});
$router->vaultPost('/api/vaults/{vault_id}/migration/start', function (RequestContext $ctx) use ($db) {
    VaultController::migrationStart($db, $ctx);
});
$router->vaultGet('/api/vaults/{vault_id}/migration/verify-source', function (RequestContext $ctx) use ($db) {
    VaultController::migrationVerifySource($db, $ctx);
});
$router->vaultPut('/api/vaults/{vault_id}/migration/commit', function (RequestContext $ctx) use ($db) {
    VaultController::migrationCommit($db, $ctx);
});

// Dispatch
$router->dispatch($_SERVER['REQUEST_METHOD'], $_SERVER['REQUEST_URI']);
