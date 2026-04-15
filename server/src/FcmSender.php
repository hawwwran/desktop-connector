<?php

/**
 * Firebase Cloud Messaging sender via HTTP v1 API.
 * Uses service account JWT for authentication. No external dependencies.
 */
class FcmSender
{
    private static ?array $serviceAccount = null;
    private static ?string $accessToken = null;
    private static int $tokenExpiry = 0;

    /**
     * Check if FCM sending is available (service account exists + openssl available).
     */
    public static function isAvailable(): bool
    {
        if (!function_exists('openssl_sign')) {
            return false;
        }
        return self::loadServiceAccount() !== null;
    }

    /**
     * Send a data-only FCM message. Returns true on success, false on any failure.
     * Never throws — safe to call fire-and-forget.
     */
    public static function sendDataMessage(string $fcmToken, array $data): bool
    {
        try {
            $sa = self::loadServiceAccount();
            if ($sa === null) {
                return false;
            }

            $accessToken = self::getAccessToken($sa);
            if ($accessToken === null) {
                return false;
            }

            $projectId = $sa['project_id'];
            $url = "https://fcm.googleapis.com/v1/projects/{$projectId}/messages:send";

            $payload = json_encode([
                'message' => [
                    'token' => $fcmToken,
                    'data' => $data,
                ],
            ]);

            $headers = [
                'Authorization: Bearer ' . $accessToken,
                'Content-Type: application/json',
            ];

            $ch = curl_init($url);
            curl_setopt_array($ch, [
                CURLOPT_POST => true,
                CURLOPT_POSTFIELDS => $payload,
                CURLOPT_HTTPHEADER => $headers,
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT => 5,
            ]);
            $response = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            return $httpCode === 200;
        } catch (\Throwable $e) {
            return false;
        }
    }

    private static function loadServiceAccount(): ?array
    {
        if (self::$serviceAccount !== null) {
            return self::$serviceAccount;
        }

        $path = __DIR__ . '/../firebase-service-account.json';
        if (!file_exists($path)) {
            return null;
        }

        $json = json_decode(file_get_contents($path), true);
        if (!$json || empty($json['private_key']) || empty($json['client_email']) || empty($json['project_id'])) {
            return null;
        }

        self::$serviceAccount = $json;
        return $json;
    }

    /**
     * Get a valid OAuth2 access token, refreshing if expired.
     */
    private static function getAccessToken(array $sa): ?string
    {
        if (self::$accessToken !== null && time() < self::$tokenExpiry) {
            return self::$accessToken;
        }

        $jwt = self::createJwt($sa);
        if ($jwt === null) {
            return null;
        }

        $ch = curl_init('https://oauth2.googleapis.com/token');
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => http_build_query([
                'grant_type' => 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion' => $jwt,
            ]),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 5,
        ]);
        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($httpCode !== 200) {
            return null;
        }

        $data = json_decode($response, true);
        if (!$data || empty($data['access_token'])) {
            return null;
        }

        self::$accessToken = $data['access_token'];
        self::$tokenExpiry = time() + ($data['expires_in'] ?? 3600) - 60; // refresh 60s early
        return self::$accessToken;
    }

    /**
     * Create a signed JWT for Google OAuth2 token exchange.
     */
    private static function createJwt(array $sa): ?string
    {
        $header = self::base64url(json_encode(['alg' => 'RS256', 'typ' => 'JWT']));

        $now = time();
        $claims = self::base64url(json_encode([
            'iss' => $sa['client_email'],
            'scope' => 'https://www.googleapis.com/auth/firebase.messaging',
            'aud' => 'https://oauth2.googleapis.com/token',
            'iat' => $now,
            'exp' => $now + 3600,
        ]));

        $signingInput = $header . '.' . $claims;

        $privateKey = openssl_pkey_get_private($sa['private_key']);
        if ($privateKey === false) {
            return null;
        }

        $signature = '';
        if (!openssl_sign($signingInput, $signature, $privateKey, OPENSSL_ALGO_SHA256)) {
            return null;
        }

        return $signingInput . '.' . self::base64url($signature);
    }

    private static function base64url(string $data): string
    {
        return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
    }
}
