package com.desktopconnector.ui.update

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.desktopconnector.ui.theme.DcBlue950
import com.desktopconnector.ui.theme.brandColors

/**
 * Top-of-app discovery banner. Visible iff the current [UpdateUiState] is
 * `Available` and the user hasn't already skipped that version. Tap opens
 * the [UpdateModal] against the existing state — no fresh network check.
 *
 * Brand-yellow surface (`accentYellow`) with `DcBlue950` text per the
 * visual identity guide. Single-line, max 56 dp tall — sits above the
 * NavHost, above whatever screen the user is on.
 */
@Composable
fun UpdateBanner(state: UpdateUiState, onClick: () -> Unit) {
    if (state !is UpdateUiState.Available || state.dismissed) return
    Surface(
        color = MaterialTheme.brandColors.accentYellow,
        contentColor = DcBlue950,
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(max = 56.dp)
            .clickable(onClick = onClick),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Text(
                "Update available — v${state.info.latestVersion}",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
            )
            Text(
                "›",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}
