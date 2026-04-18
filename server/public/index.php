<?php

// Desktop Connector - PHP Relay Server

require_once __DIR__ . '/../src/Database.php';
require_once __DIR__ . '/../src/Router.php';
require_once __DIR__ . '/../src/Controllers/DeviceController.php';
require_once __DIR__ . '/../src/Controllers/PairingController.php';
require_once __DIR__ . '/../src/Controllers/TransferController.php';
require_once __DIR__ . '/../src/Controllers/DashboardController.php';
require_once __DIR__ . '/../src/Controllers/FcmController.php';
require_once __DIR__ . '/../src/Controllers/FasttrackController.php';
require_once __DIR__ . '/../src/FcmSender.php';
require_once __DIR__ . '/../src/AppLog.php';

// --- Services ---
require_once __DIR__ . '/../src/Services/TransferStatusService.php';
require_once __DIR__ . '/../src/Services/TransferNotifyService.php';
require_once __DIR__ . '/../src/Services/TransferWakeService.php';
require_once __DIR__ . '/../src/Services/TransferCleanupService.php';

// Initialize database
$db = Database::getInstance();
$db->migrate();

$router = new Router();

// --- Public routes (no auth) ---

$router->get('/api/health', function () use ($db) {
    DeviceController::health($db);
});

$router->post('/api/devices/register', function () use ($db) {
    DeviceController::register($db);
});

$router->get('/api/fcm/config', function () {
    FcmController::config();
});

$router->get('/api/devices/stats', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    DeviceController::stats($db, $deviceId);
});

$router->get('/dashboard', function () use ($db) {
    DashboardController::show($db);
});

// Redirect root to dashboard
$router->get('/', function () {
    header('Location: dashboard');
    exit;
});

// --- Authenticated routes ---

$router->post('/api/devices/fcm-token', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    DeviceController::updateFcmToken($db, $deviceId);
});

$router->post('/api/devices/ping', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    DeviceController::ping($db, $deviceId);
});

$router->post('/api/devices/pong', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    DeviceController::pong($db, $deviceId);
});

$router->post('/api/pairing/request', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    PairingController::request($db, $deviceId);
});

$router->get('/api/pairing/poll', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    PairingController::poll($db, $deviceId);
});

$router->post('/api/pairing/confirm', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    PairingController::confirm($db, $deviceId);
});

$router->post('/api/transfers/init', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::init($db, $deviceId);
});

$router->post('/api/transfers/{transfer_id}/chunks/{chunk_index}', function ($params) use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::uploadChunk($db, $deviceId, $params);
});

$router->get('/api/transfers/pending', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::pending($db, $deviceId);
});

$router->get('/api/transfers/{transfer_id}/chunks/{chunk_index}', function ($params) use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::downloadChunk($db, $deviceId, $params);
});

$router->post('/api/transfers/{transfer_id}/ack', function ($params) use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::ack($db, $deviceId, $params);
});

$router->get('/api/transfers/sent-status', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::sentStatus($db, $deviceId);
});

$router->get('/api/transfers/notify', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    TransferController::notify($db, $deviceId);
});

// --- Fasttrack: lightweight encrypted message relay ---

$router->post('/api/fasttrack/send', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    FasttrackController::send($db, $deviceId);
});

$router->get('/api/fasttrack/pending', function () use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    FasttrackController::pending($db, $deviceId);
});

$router->post('/api/fasttrack/{id}/ack', function ($params) use ($db) {
    $deviceId = Router::authenticate($db);
    if ($deviceId === null) return;
    FasttrackController::ack($db, $deviceId, $params);
});

// Dispatch
$router->dispatch($_SERVER['REQUEST_METHOD'], $_SERVER['REQUEST_URI']);
