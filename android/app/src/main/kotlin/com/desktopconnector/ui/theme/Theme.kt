package com.desktopconnector.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat
import com.desktopconnector.data.ThemeMode

/**
 * Brand-semantic colors that don't fit Material3's ColorScheme slots
 * (connection states, warm accent, progress-bar family).
 */
data class BrandColors(
    val connectionConnected: Color,
    val connectionReconnecting: Color,
    val connectionDisconnected: Color,
    val transferOutgoing: Color,
    val transferIncoming: Color,
    val transferDelivering: Color,
    val accentYellow: Color,
)

private val DarkBrandColors = BrandColors(
    connectionConnected = DcBlue500,
    connectionReconnecting = DcYellow500,
    connectionDisconnected = DcBlue200,
    transferOutgoing = DcYellow500,
    transferIncoming = DcBlue400,
    transferDelivering = DcBlue500,
    accentYellow = DcYellow500,
)

private val LightBrandColors = BrandColors(
    connectionConnected = DcBlue500,
    connectionReconnecting = DcYellow600,
    connectionDisconnected = DcBlue400,
    transferOutgoing = DcYellow600,
    transferIncoming = DcBlue800,
    transferDelivering = DcBlue500,
    accentYellow = DcYellow600,
)

val LocalBrandColors = staticCompositionLocalOf { DarkBrandColors }

val MaterialTheme.brandColors: BrandColors
    @Composable
    @ReadOnlyComposable
    get() = LocalBrandColors.current

private val DarkColorScheme = darkColorScheme(
    primary = DcBlue700,
    onPrimary = DcWhiteSoft,
    primaryContainer = DcBlue500,
    onPrimaryContainer = DcWhiteSoft,
    secondary = DcBlue400,
    onSecondary = DcBlue950,
    secondaryContainer = DcBlue800,
    onSecondaryContainer = DcWhiteSoft,
    tertiary = DcYellow500,
    onTertiary = DcBlue950,
    tertiaryContainer = DcYellow600,
    onTertiaryContainer = DcBlue950,
    background = DcBlue970,
    onBackground = DcWhiteSoft,
    surface = DcBlue950,
    onSurface = DcWhiteSoft,
    surfaceVariant = DcBlue900,
    onSurfaceVariant = DcBlue200,
    error = DcOrange700,
    onError = Color.White,
    errorContainer = Color(0xFF5C2800),
    onErrorContainer = DcWhiteSoft,
    outline = DcBlue500,
    outlineVariant = DcBlue700,
)

private val LightColorScheme = lightColorScheme(
    primary = DcBlue800,
    onPrimary = DcWhiteSoft,
    primaryContainer = DcBlue500,
    onPrimaryContainer = DcBlue950,
    secondary = DcBlue500,
    onSecondary = DcWhiteSoft,
    secondaryContainer = DcBlue200,
    onSecondaryContainer = DcBlue950,
    tertiary = DcYellow600,
    onTertiary = DcBlue950,
    tertiaryContainer = DcYellow500,
    onTertiaryContainer = DcBlue950,
    background = DcWhiteSoft,
    onBackground = DcBlue950,
    surface = Color.White,
    onSurface = DcBlue950,
    surfaceVariant = DcBlue200,
    onSurfaceVariant = DcBlue800,
    error = DcOrange700,
    onError = Color.White,
    errorContainer = Color(0xFFFFE0B2),
    onErrorContainer = Color(0xFF5C2800),
    outline = DcBlue500,
    outlineVariant = DcBlue200,
)

@Composable
fun DesktopConnectorTheme(
    themeMode: ThemeMode = ThemeMode.SYSTEM,
    content: @Composable () -> Unit,
) {
    val useDark = when (themeMode) {
        ThemeMode.SYSTEM -> isSystemInDarkTheme()
        ThemeMode.LIGHT -> false
        ThemeMode.DARK -> true
    }
    val colors = if (useDark) DarkColorScheme else LightColorScheme
    val brand = if (useDark) DarkBrandColors else LightBrandColors

    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as? Activity)?.window ?: return@SideEffect
            window.statusBarColor = colors.background.toArgb()
            window.navigationBarColor = colors.background.toArgb()
            val insetsController = WindowCompat.getInsetsController(window, view)
            insetsController.isAppearanceLightStatusBars = !useDark
            insetsController.isAppearanceLightNavigationBars = !useDark
        }
    }

    CompositionLocalProvider(LocalBrandColors provides brand) {
        MaterialTheme(colorScheme = colors, content = content)
    }
}
