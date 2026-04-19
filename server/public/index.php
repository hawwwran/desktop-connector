<?php

// Desktop Connector - PHP Relay Server

require_once __DIR__ . '/../src/Database.php';

// --- Http pipeline ---
require_once __DIR__ . '/../src/Http/RequestContext.php';
require_once __DIR__ . '/../src/Http/ApiError.php';
require_once __DIR__ . '/../src/Http/ErrorResponder.php';
require_once __DIR__ . '/../src/Http/Validators.php';

// --- Auth ---
require_once __DIR__ . '/../src/Auth/AuthIdentity.php';
require_once __DIR__ . '/../src/Auth/AuthService.php';

// --- Repositories ---
require_once __DIR__ . '/../src/Repositories/DeviceRepository.php';
require_once __DIR__ . '/../src/Repositories/PairingRepository.php';
require_once __DIR__ . '/../src/Repositories/TransferRepository.php';
require_once __DIR__ . '/../src/Repositories/ChunkRepository.php';
require_once __DIR__ . '/../src/Repositories/FasttrackRepository.php';
require_once __DIR__ . '/../src/Repositories/PingRateRepository.php';

require_once __DIR__ . '/../src/Router.php';

// --- Controllers ---
require_once __DIR__ . '/../src/Controllers/DeviceController.php';
require_once __DIR__ . '/../src/Controllers/PairingController.php';
require_once __DIR__ . '/../src/Controllers/TransferController.php';
require_once __DIR__ . '/../src/Controllers/DashboardController.php';
require_once __DIR__ . '/../src/Controllers/FcmController.php';
require_once __DIR__ . '/../src/Controllers/FasttrackController.php';

require_once __DIR__ . '/../src/FcmSender.php';
require_once __DIR__ . '/../src/AppLog.php';

// --- Transfer domain ---
require_once __DIR__ . '/../src/Domain/Transfer/TransferState.php';
require_once __DIR__ . '/../src/Domain/Transfer/TransferLifecycle.php';

// --- Services ---
require_once __DIR__ . '/../src/Services/TransferStatusService.php';
require_once __DIR__ . '/../src/Services/TransferNotifyService.php';
require_once __DIR__ . '/../src/Services/TransferWakeService.php';
require_once __DIR__ . '/../src/Services/TransferCleanupService.php';
require_once __DIR__ . '/../src/Services/TransferService.php';

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

// Dispatch
$router->dispatch($_SERVER['REQUEST_METHOD'], $_SERVER['REQUEST_URI']);
