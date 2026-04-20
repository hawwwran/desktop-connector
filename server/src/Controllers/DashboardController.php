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
        // FCM column is only meaningful when this server can actually push.
        // Without service-account.json the whole row disappears — no point
        // scolding phones for missing tokens on a server that can't use them.
        $fcmAvailable = FcmSender::isAvailable();
        echo self::render($devices, $pairings, $pendingTransfers, $stats, $now, $fcmAvailable);
    }

    private static function render(array $devices, array $pairings, array $transfers,
                                   ?array $stats, int $now, bool $fcmAvailable): string
    {
        $deviceCount = $stats['device_count'] ?? 0;
        $pairingCount = $stats['pairing_count'] ?? 0;
        $pendingCount = $stats['pending_count'] ?? 0;
        $uploadingCount = $stats['uploading_count'] ?? 0;
        $storageBytes = $stats['storage_bytes'] ?? 0;
        $storageMB = round($storageBytes / (1024 * 1024), 2);
        $quotaMB = (int)Config::get('storageQuotaMB');
        $quotaBytes = $quotaMB * 1024 * 1024;
        // Threshold colour: orange (full) at >=100%, yellow at >=80%,
        // brand blue otherwise. Gives operators a quick visual cue that
        // new incoming transfers are at risk of 507.
        if ($quotaBytes > 0 && $storageBytes >= $quotaBytes) {
            $storageColour = '#EA7601';
        } elseif ($quotaBytes > 0 && $storageBytes >= 0.8 * $quotaBytes) {
            $storageColour = '#FDD00C';
        } else {
            $storageColour = '#ffffff';
        }
        $storageDisplay = sprintf('%.1f / %d MB', $storageMB, $quotaMB);
        $version = self::serverVersion();
        $versionChip = $version !== null ? ('v' . htmlspecialchars($version) . ' &middot; ') : '';

        $deviceRows = '';
        foreach ($devices as $d) {
            $age = self::timeAgo($now - $d['last_seen_at']);
            $created = date('Y-m-d H:i', $d['created_at']);
            $type = htmlspecialchars($d['device_type']);
            $id = htmlspecialchars(substr($d['device_id'], 0, 12) . '...');
            $fullId = htmlspecialchars($d['device_id']);
            $online = ($now - $d['last_seen_at']) < 120;
            $statusDot = $online
                ? '<span style="color:#3986FC">&#9679;</span> online'
                : '<span style="color:#EA7601">&#9679;</span> ' . $age . ' ago';
            $fcmCell = '';
            if ($fcmAvailable) {
                $hasToken = !empty($d['fcm_token']);
                if ($hasToken) {
                    // Brand blue — token on record, push wake available.
                    // Suffix with the last successful push so operators can
                    // distinguish "registered, never pushed" from "pushes
                    // actively working". "never" = token registered but no
                    // push has succeeded since the column was added.
                    $lastOk = (int)($d['fcm_last_success_at'] ?? 0);
                    $freshness = $lastOk > 0
                        ? self::timeAgo($now - $lastOk) . ' ago'
                        : 'never';
                    $fcmCell = '<td><span style="color:#3986FC">&#9679;</span> ready'
                        . ' <span style="color:#A4D0FB">&middot; ' . $freshness . '</span></td>';
                } elseif ($d['device_type'] === 'phone') {
                    // Orange — phone without a token is a problem; pings
                    // come back no_token and the desktop sees the phone
                    // as offline.
                    $fcmCell = '<td><span style="color:#EA7601">&#9679;</span> no token</td>';
                } else {
                    // Desktops don't register FCM tokens — dim dash.
                    $fcmCell = '<td style="color:#5898FB">&mdash;</td>';
                }
            }
            $deviceRows .= "<tr>
                <td title=\"{$fullId}\">{$id}</td>
                <td>{$type}</td>
                <td>{$statusDot}</td>
                {$fcmCell}
                <td>{$created}</td>
            </tr>";
        }
        $fcmHeader = $fcmAvailable ? '<th>FCM</th>' : '';

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
            $status = $t['complete'] ? 'ready' : 'uploading';
            $statusColor = $t['complete'] ? '#3986FC' : '#FDD00C';
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
    <title>Desktop Connector &mdash; Relay Server</title>
    <meta http-equiv="refresh" content="5">
    <link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png">
    <link rel="icon" type="image/png" sizes="64x64" href="favicon-64.png">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #000733; color: #E8EEFD; padding: 24px; }
        h1 { color: #ffffff; margin-bottom: 8px; font-size: 1.5rem;
             display: flex; align-items: center; gap: 10px; }
        h1 .spark { width: 22px; height: 22px; flex: none; }
        .subtitle { color: #A4D0FB; margin-bottom: 24px; font-size: 0.875rem; }
        .stats { display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }
        .stat { background: #00146C; border-radius: 8px; padding: 16px 24px; min-width: 140px; }
        .stat-value { font-size: 1.5rem; font-weight: 700; color: #ffffff; }
        .stat-label { font-size: 0.75rem; color: #A4D0FB; text-transform: uppercase; letter-spacing: 0.05em; }
        h2 { color: #ffffff; margin: 24px 0 12px; font-size: 1.1rem; }
        table { width: 100%; border-collapse: collapse; background: #00146C; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }
        th { background: #0A1C7A; color: #A4D0FB; font-size: 0.75rem; text-transform: uppercase;
             letter-spacing: 0.05em; padding: 10px 14px; text-align: left; }
        td { padding: 10px 14px; border-top: 1px solid #0A1C7A; font-size: 0.875rem; font-family: 'SF Mono', monospace; }
        tr:hover td { background: #0920AC; }
        .empty { color: #5898FB; padding: 24px; text-align: center; }
    </style>
</head>
<body>
    <h1>
        <svg class="spark" viewBox="0 0 24 24" fill="#3986FC" aria-hidden="true">
            <path d="M12 0 L14 10 L24 12 L14 14 L12 24 L10 14 L0 12 L10 10 Z"/>
        </svg>
        Desktop Connector &mdash; Relay Server
    </h1>
    <div class="subtitle">{$versionChip}auto-refreshes every 5s</div>

    <div class="stats">
        <div class="stat"><div class="stat-value">{$deviceCount}</div><div class="stat-label">Devices</div></div>
        <div class="stat"><div class="stat-value">{$pairingCount}</div><div class="stat-label">Pairings</div></div>
        <div class="stat"><div class="stat-value">{$pendingCount}</div><div class="stat-label">Pending transfers</div></div>
        <div class="stat"><div class="stat-value">{$uploadingCount}</div><div class="stat-label">Uploading</div></div>
        <div class="stat"><div class="stat-value" style="color:{$storageColour}">{$storageDisplay}</div><div class="stat-label">Storage used</div></div>
    </div>

    <h2>Devices</h2>
    <table>
        <tr><th>Device ID</th><th>Type</th><th>Status</th>{$fcmHeader}<th>Registered</th></tr>
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

    private static function serverVersion(): ?string
    {
        // server/VERSION.md ships with the deploy tree and is the authoritative
        // release marker (bumped on every release). YAML frontmatter with a
        // `version: X.Y.Z` line.
        $path = __DIR__ . '/../../VERSION.md';
        if (!is_file($path)) return null;
        $body = (string)@file_get_contents($path);
        if ($body === '') return null;
        if (preg_match('/^version:\s*([^\s]+)\s*$/m', $body, $m)) {
            return $m[1];
        }
        return null;
    }

    private static function formatBytes(int $bytes): string
    {
        if ($bytes < 1024) return $bytes . ' B';
        if ($bytes < 1024 * 1024) return round($bytes / 1024, 1) . ' KB';
        if ($bytes < 1024 * 1024 * 1024) return round($bytes / (1024 * 1024), 1) . ' MB';
        return round($bytes / (1024 * 1024 * 1024), 2) . ' GB';
    }
}
