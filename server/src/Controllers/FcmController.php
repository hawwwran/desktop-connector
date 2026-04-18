<?php

class FcmController
{
    /**
     * GET /api/fcm/config — return Firebase client config for dynamic initialization.
     * Public endpoint (no auth needed — values are non-secret client identifiers).
     */
    public static function config(Database $db, RequestContext $ctx): void
    {
        $path = __DIR__ . '/../../google-services.json';
        if (!file_exists($path)) {
            Router::json(['available' => false]);
            return;
        }

        $json = json_decode(file_get_contents($path), true);
        if (!$json || empty($json['project_info']) || empty($json['client'])) {
            Router::json(['available' => false]);
            return;
        }

        $client = $json['client'][0] ?? null;
        if (!$client) {
            Router::json(['available' => false]);
            return;
        }

        Router::json([
            'available' => true,
            'project_id' => $json['project_info']['project_id'] ?? '',
            'gcm_sender_id' => $json['project_info']['project_number'] ?? '',
            'application_id' => $client['client_info']['mobilesdk_app_id'] ?? '',
            'api_key' => $client['api_key'][0]['current_key'] ?? '',
        ]);
    }
}
