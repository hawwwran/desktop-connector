package com.desktopconnector.messaging

class MessageDispatcher {
    private val handlers = mutableMapOf<MessageType, (DeviceMessage) -> Unit>()

    fun register(type: MessageType, handler: (DeviceMessage) -> Unit) {
        handlers[type] = handler
    }

    fun dispatch(message: DeviceMessage): Boolean {
        val handler = handlers[message.type] ?: return false
        handler(message)
        return true
    }
}
