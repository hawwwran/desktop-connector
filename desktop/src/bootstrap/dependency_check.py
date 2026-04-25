"""Dependency checks and fallback install UI for desktop bootstrap.

The missing-deps UI itself is platform-sensitive. We route installer
launch through the composed platform shell backend so the flow remains
platform-neutral as additional desktop runtimes are introduced.
"""

from __future__ import annotations

from ..interfaces.shell import ShellBackend
from ..platform.compose import compose_desktop_platform

def check_dependencies(*, headless: bool = False) -> list[tuple[str, str]]:
    """Check required dependencies for the planned startup mode.

    Headless receivers (no tray, no GUI pairing, no subprocess windows)
    skip pystray / tkinter / PIL.ImageTk / GTK4. Lets a minimal AppImage
    that bundles only Python + pure-Python deps run --headless without
    tripping on missing system GTK4. qrcode stays in the always-on set
    since pairing can fire from any startup mode.
    """
    missing: list[tuple[str, str]] = []

    core = [
        ("nacl", "PyNaCl", "python3-nacl or: pip install PyNaCl"),
        ("cryptography", "cryptography", "pip install cryptography"),
        ("requests", "requests", "pip install requests"),
        ("PIL", "Pillow", "python3-pil or: pip install Pillow"),
        ("qrcode", "qrcode", "pip install --user --break-system-packages qrcode"),
    ]
    for module, name, fix in core:
        try:
            __import__(module)
        except ImportError:
            missing.append((name, fix))

    if headless:
        return missing

    try:
        __import__("pystray")
    except ImportError:
        missing.append(
            ("pystray", "pip install --user --break-system-packages pystray")
        )

    try:
        import tkinter  # noqa: F401
    except ImportError:
        missing.append(("tkinter", "sudo apt install python3-tk"))

    try:
        from PIL import ImageTk  # noqa: F401
    except ImportError:
        missing.append(("Pillow-ImageTk", "sudo apt install python3-pil.imagetk"))

    # GTK4/libadwaita check forks a subprocess to avoid GTK3/4 conflict
    # with pystray in the main process.
    import subprocess as _sp

    result = _sp.run(
        [
            "python3",
            "-c",
            "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        missing.append(
            (
                "GTK4/libadwaita",
                "sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
            )
        )

    return missing


def show_missing_deps_dialog(missing: list[tuple[str, str]]) -> None:
    """Show a dialog about missing dependencies with install button."""
    # Compose the platform once; pass only the shell backend to the UI
    # helpers so the installer button can be fired without rebuilding
    # every backend on each click.
    shell = compose_desktop_platform().shell
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401

        _show_deps_gtk4(missing, shell)
    except Exception:
        try:
            _show_deps_tkinter(missing, shell)
        except Exception:
            # Last resort: print to terminal
            print("\nMissing dependencies:")
            for name, fix in missing:
                print(f"  - {name}: {fix}")
            print("\nRun the installer to fix:")
            print(
                "  curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash\n"
            )


def _show_deps_gtk4(missing: list[tuple[str, str]], shell: ShellBackend) -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    app = Adw.Application(application_id="com.desktopconnector.deps")

    def on_activate(app):
        win = Adw.ApplicationWindow(
            application=app,
            title="Desktop Connector",
            default_width=400,
            default_height=300,
        )
        toolbar = Adw.ToolbarView()
        win.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        toolbar.set_content(box)

        label = Gtk.Label(label="Missing dependencies", xalign=0)
        label.add_css_class("title-3")
        box.append(label)

        for name, fix in missing:
            row = Gtk.Label(label=f"• {name}\n  {fix}", xalign=0, wrap=True)
            row.add_css_class("body")
            box.append(row)

        def on_install(_btn):
            shell.launch_installer_terminal(
                "curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash; echo; read -p 'Press Enter to close...'"
            )
            win.close()

        install_btn = Gtk.Button(label="Install Dependencies")
        install_btn.add_css_class("suggested-action")
        install_btn.connect("clicked", on_install)
        box.append(install_btn)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)


def _show_deps_tkinter(missing: list[tuple[str, str]], shell: ShellBackend) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Desktop Connector — Missing Dependencies")
    root.configure(bg="#1e293b")

    frame = tk.Frame(root, bg="#1e293b", padx=24, pady=24)
    frame.pack()

    tk.Label(
        frame,
        text="Missing dependencies",
        font=("sans-serif", 14, "bold"),
        fg="#f8fafc",
        bg="#1e293b",
    ).pack(anchor=tk.W, pady=(0, 12))

    for name, fix in missing:
        tk.Label(
            frame,
            text=f"• {name}: {fix}",
            font=("sans-serif", 10),
            fg="#94a3b8",
            bg="#1e293b",
            anchor=tk.W,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=2)

    def on_install():
        shell.launch_installer_terminal(
            "curl -fsSL https://raw.githubusercontent.com/hawwwran/desktop-connector/main/desktop/install.sh | bash; echo; read -p 'Press Enter to close...'"
        )
        root.destroy()

    tk.Button(
        frame,
        text="Install Dependencies",
        command=on_install,
        font=("sans-serif", 11),
        bg="#3b82f6",
        fg="#f8fafc",
        padx=16,
        pady=6,
    ).pack(pady=(16, 0))

    root.mainloop()
