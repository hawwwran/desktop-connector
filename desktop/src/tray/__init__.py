"""Public surface of the tray icon + menu.

Composed from topical mixins under this package; legacy ``from .tray
import TrayApp`` imports keep working unchanged because Python
resolves ``tray`` as this package and finds ``TrayApp`` in the
package namespace.
"""

from .app import TrayApp

__all__ = ["TrayApp"]
