package com.desktopconnector.messaging

enum class MessageType {
    CLIPBOARD_TEXT,
    CLIPBOARD_IMAGE,
    PAIRING_UNPAIR,
    FIND_PHONE_START,
    FIND_PHONE_STOP,
    FIND_PHONE_LOCATION_UPDATE,
}

enum class MessageTransport {
    TRANSFER_FILE,
    FASTTRACK,
}

data class DeviceMessage(
    val type: MessageType,
    val transport: MessageTransport,
    val payload: Map<String, Any?> = emptyMap(),
    val senderId: String? = null,
    val recipientId: String? = null,
    val metadata: Map<String, Any?> = emptyMap(),
)
