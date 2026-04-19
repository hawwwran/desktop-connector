<?php

class DashboardController
{
    public static function show(Database $db): void
    {
        $devices = (new DeviceRepository($db))->findAll();
        $pairings = (new PairingRepository($db))->findAll();
        $transfers = new TransferRepository($db);
        $chunks = new ChunkRepository($db);

        $pendingTransfers = $transfers->listPendingForDashboard();
        foreach ($pendingTransfers as &$t) {
            $t['total_bytes'] = $chunks->sumChunkBytesForTransfer($t['id']);
        }
        unset($t);

        $stats = [
            'device_count' => count($devices),
            'pairing_count' => count($pairings),
            'pending_count' => $transfers->countPendingByCompleteDownloaded(1, 0),
            'uploading_count' => $transfers->countPendingByCompleteDownloaded(0, 0),
            'storage_bytes' => $chunks->sumAllBytes(),
        ];

        http_response_code(200);
        header('Content-Type: text/html; charset=utf-8');

        $now = time();
        echo self::render($devices, $pairings, $pendingTransfers, $stats, $now);
    }

    private static function render(array $devices, array $pairings, array $transfers, ?array $stats, int $now): string
    {
        $deviceCount = $stats['device_count'] ?? 0;
        $pairingCount = $stats['pairing_count'] ?? 0;
        $pendingCount = $stats['pending_count'] ?? 0;
        $uploadingCount = $stats['uploading_count'] ?? 0;
        $storageBytes = $stats['storage_bytes'] ?? 0;
        $storageMB = round($storageBytes / (1024 * 1024), 2);

        $deviceRows = '';
        foreach ($devices as $d) {
            $age = self::timeAgo($now - $d['last_seen_at']);
            $created = date('Y-m-d H:i', $d['created_at']);
            $type = htmlspecialchars($d['device_type']);
            $id = htmlspecialchars(substr($d['device_id'], 0, 12) . '...');
            $fullId = htmlspecialchars($d['device_id']);
            $online = ($now - $d['last_seen_at']) < 120;
            $statusDot = $online
                ? '<span style="color:#22c55e">&#9679;</span> online'
                : '<span style="color:#ef4444">&#9679;</span> ' . $age . ' ago';
            $deviceRows .= "<tr>
                <td title=\"{$fullId}\">{$id}</td>
                <td>{$type}</td>
                <td>{$statusDot}</td>
                <td>{$created}</td>
            </tr>";
        }

        $pairingRows = '';
        foreach ($pairings as $p) {
            $a = htmlspecialchars(substr($p['device_a_id'], 0, 12) . '...');
            $b = htmlspecialchars(substr($p['device_b_id'], 0, 12) . '...');
            $bytes = self::formatBytes($p['bytes_transferred']);
            $count = (int)$p['transfer_count'];
            $since = date('Y-m-d H:i', $p['created_at']);
            $pairingRows .= "<tr>
                <td>{$a}</td>
                <td>{$b}</td>
                <td>{$count}</td>
                <td>{$bytes}</td>
                <td>{$since}</td>
            </tr>";
        }

        $transferRows = '';
        foreach ($transfers as $t) {
            $tid = htmlspecialchars(substr($t['id'], 0, 12) . '...');
            $from = htmlspecialchars(substr($t['sender_id'], 0, 12) . '...');
            $to = htmlspecialchars(substr($t['recipient_id'], 0, 12) . '...');
            $chunks = (int)$t['chunks_received'] . '/' . (int)$t['chunk_count'];
            $bytes = self::formatBytes($t['total_bytes']);
            $age = self::timeAgo($now - $t['created_at']);
            $state = TransferLifecycle::deriveState($t);
            $isReady = $state->is(TransferState::UPLOADED) || $state->is(TransferState::DELIVERING);
            $status = $isReady ? 'ready' : 'uploading';
            $statusColor = $isReady ? '#22c55e' : '#f59e0b';
            $transferRows .= "<tr>
                <td>{$tid}</td>
                <td>{$from}</td>
                <td>{$to}</td>
                <td><span style=\"color:{$statusColor}\">{$status}</span></td>
                <td>{$chunks}</td>
                <td>{$bytes}</td>
                <td>{$age} ago</td>
            </tr>";
        }

        return <<<HTML
<!DOCTYPE html>
<html>
<head>
    <title>Desktop Connector Dashboard</title>
    <meta http-equiv="refresh" content="5">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0f172a; color: #e2e8f0; padding: 24px; }
        h1 { color: #f8fafc; margin-bottom: 8px; font-size: 1.5rem; }
        .subtitle { color: #94a3b8; margin-bottom: 24px; font-size: 0.875rem; }
        .stats { display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }
        .stat { background: #1e293b; border-radius: 8px; padding: 16px 24px; min-width: 140px; }
        .stat-value { font-size: 1.5rem; font-weight: 700; color: #f8fafc; }
        .stat-label { font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
        h2 { color: #f8fafc; margin: 24px 0 12px; font-size: 1.1rem; }
        table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }
        th { background: #334155; color: #94a3b8; font-size: 0.75rem; text-transform: uppercase;
             letter-spacing: 0.05em; padding: 10px 14px; text-align: left; }
        td { padding: 10px 14px; border-top: 1px solid #334155; font-size: 0.875rem; font-family: 'SF Mono', monospace; }
        tr:hover td { background: #263048; }
        .empty { color: #64748b; padding: 24px; text-align: center; }
    </style>
</head>
<body>
    <h1>Desktop Connector</h1>
    <div class="subtitle">Relay server dashboard &mdash; auto-refreshes every 5s</div>

    <div class="stats">
        <div class="stat"><div class="stat-value">{$deviceCount}</div><div class="stat-label">Devices</div></div>
        <div class="stat"><div class="stat-value">{$pairingCount}</div><div class="stat-label">Pairings</div></div>
        <div class="stat"><div class="stat-value">{$pendingCount}</div><div class="stat-label">Pending transfers</div></div>
        <div class="stat"><div class="stat-value">{$uploadingCount}</div><div class="stat-label">Uploading</div></div>
        <div class="stat"><div class="stat-value">{$storageMB} MB</div><div class="stat-label">Storage used</div></div>
    </div>

    <h2>Devices</h2>
    <table>
        <tr><th>Device ID</th><th>Type</th><th>Status</th><th>Registered</th></tr>
        {$deviceRows}
    </table>

    <h2>Pairings</h2>
    <table>
        <tr><th>Device A</th><th>Device B</th><th>Transfers</th><th>Data</th><th>Since</th></tr>
        {$pairingRows}
    </table>

    <h2>Transfer Queue</h2>
    <table>
        <tr><th>Transfer ID</th><th>From</th><th>To</th><th>Status</th><th>Chunks</th><th>Size</th><th>Age</th></tr>
        {$transferRows}
    </table>
</body>
</html>
HTML;
    }

    private static function timeAgo(int $seconds): string
    {
        if ($seconds < 60) return $seconds . 's';
        if ($seconds < 3600) return floor($seconds / 60) . 'm';
        if ($seconds < 86400) return floor($seconds / 3600) . 'h';
        return floor($seconds / 86400) . 'd';
    }

    private static function formatBytes(int $bytes): string
    {
        if ($bytes < 1024) return $bytes . ' B';
        if ($bytes < 1024 * 1024) return round($bytes / 1024, 1) . ' KB';
        if ($bytes < 1024 * 1024 * 1024) return round($bytes / (1024 * 1024), 1) . ' MB';
        return round($bytes / (1024 * 1024 * 1024), 2) . ' GB';
    }
}
