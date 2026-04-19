package com.desktopconnector.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import com.desktopconnector.network.ConnectionState
import com.desktopconnector.ui.theme.brandColors

@Composable
fun StatusBar(
    connectionState: ConnectionState,
    statusText: String,
    onTryAgain: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val brand = MaterialTheme.brandColors
    val dotColor = when (connectionState) {
        ConnectionState.CONNECTED -> brand.connectionConnected
        ConnectionState.RECONNECTING -> brand.connectionReconnecting
        ConnectionState.DISCONNECTED -> brand.connectionDisconnected
    }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(dotColor)
        )
        Spacer(Modifier.width(10.dp))
        Text(
            text = statusText,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f),
        )
        if (connectionState == ConnectionState.DISCONNECTED) {
            TextButton(onClick = onTryAgain) {
                Text("Try Again")
            }
        }
    }
}
