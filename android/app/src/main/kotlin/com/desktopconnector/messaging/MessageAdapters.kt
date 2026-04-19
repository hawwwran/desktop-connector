package com.desktopconnector.messaging

import org.json.JSONObject

object MessageAdapters {
    fun fromFnTransfer(fileName: String, data: ByteArray, senderId: String? = null): DeviceMessage? {
        val parts = fileName.split('.')
        if (parts.size < 3 || parts[1] != "fn") return null

        return when (parts[2]) {
            "clipboard" -> {
                val subtype = parts.getOrElse(3) { "text" }
                when (subtype) {
                    "text" -> DeviceMessage(
                        type = MessageType.CLIPBOARD_TEXT,
                        transport = MessageTransport.TRANSFER_FILE,
                        payload = mapOf("text" to String(data)),
                        senderId = senderId,
                        metadata = mapOf("filename" to fileName),
                    )
                    "image" -> DeviceMessage(
                        type = MessageType.CLIPBOARD_IMAGE,
                        transport = MessageTransport.TRANSFER_FILE,
                        payload = mapOf("image_bytes" to data),
                        senderId = senderId,
                        metadata = mapOf("filename" to fileName),
                    )
                    else -> null
                }
            }
            "unpair" -> DeviceMessage(
                type = MessageType.PAIRING_UNPAIR,
                transport = MessageTransport.TRANSFER_FILE,
                senderId = senderId,
                metadata = mapOf("filename" to fileName),
            )
            else -> null
        }
    }

    fun fromFasttrackPayload(payload: JSONObject, senderId: String? = null): DeviceMessage? {
        if (payload.optString("fn") != "find-phone") return null

        val messageType = when (payload.optString("action")) {
            "start" -> MessageType.FIND_PHONE_START
            "stop" -> MessageType.FIND_PHONE_STOP
            else -> when (payload.optString("state")) {
                "ringing", "stopped" -> MessageType.FIND_PHONE_LOCATION_UPDATE
                else -> return null
            }
        }

        val map = mutableMapOf<String, Any?>()
        payload.keys().forEach { key -> map[key] = payload.opt(key) }

        return DeviceMessage(
            type = messageType,
            transport = MessageTransport.FASTTRACK,
            payload = map,
            senderId = senderId,
        )
    }
}
