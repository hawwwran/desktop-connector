"""
Pairing flow: QR code display + verification code confirmation.
"""

import base64
import json
import logging
import time

import qrcode
from PIL import Image

from .api_client import ApiClient
from .config import Config
from .crypto import KeyManager

log = logging.getLogger(__name__)


def _get_lan_server_url(config: Config) -> str:
    """Replace localhost with the machine's LAN IP so phones can reach it."""
    import socket
    url = config.server_url
    if "localhost" in url or "127.0.0.1" in url:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
            url = url.replace("localhost", lan_ip).replace("127.0.0.1", lan_ip)
            log.info("QR code will use LAN IP: %s", url)
        except Exception:
            log.warning("Could not detect LAN IP, using config URL as-is")
    return url


def generate_qr_data(config: Config, crypto: KeyManager) -> str:
    """Generate the JSON payload for the QR code."""
    server_url = _get_lan_server_url(config)
    return json.dumps({
        "server": server_url,
        "device_id": crypto.get_device_id(),
        "pubkey": crypto.get_public_key_b64(),
        "name": config.device_name,
    }, separators=(",", ":"))


def generate_qr_image(data: str) -> Image.Image:
    """Generate a QR code PIL Image from data string."""
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def run_pairing_gui(config: Config, crypto: KeyManager, api: ApiClient) -> bool:
    """
    Show QR code in a tkinter window and wait for phone to pair.
    Returns True if pairing completed, False if cancelled.
    """
    import tkinter as tk
    from PIL import ImageTk

    qr_data = generate_qr_data(config, crypto)
    qr_image = generate_qr_image(qr_data)
    log.info("QR data: %s", qr_data)
    log.info("Server URL for phone: %s", json.loads(qr_data)["server"])

    paired = [False]
    verification_code = [None]
    phone_info = [None]

    root = tk.Tk()
    root.title("Desktop Connector - Pairing")
    root.configure(bg="#1e293b")
    root.resizable(False, False)

    frame = tk.Frame(root, bg="#1e293b", padx=24, pady=24)
    frame.pack()

    tk.Label(frame, text="Scan this QR code with your phone", font=("sans-serif", 14, "bold"),
             fg="#f8fafc", bg="#1e293b").pack(pady=(0, 4))
    server_url = json.loads(qr_data)["server"]
    tk.Label(frame, text=f"Server: {server_url}",
             font=("monospace", 11), fg="#93c5fd", bg="#1e293b").pack(pady=(0, 4))
    tk.Label(frame, text=f"Device ID: {crypto.get_device_id()[:16]}...",
             font=("monospace", 10), fg="#94a3b8", bg="#1e293b").pack(pady=(0, 12))

    qr_photo = ImageTk.PhotoImage(qr_image)
    qr_label = tk.Label(frame, image=qr_photo, bg="#1e293b")
    qr_label.pack(pady=(0, 16))

    status_label = tk.Label(frame, text="Waiting for phone to scan...",
                            font=("sans-serif", 11), fg="#f59e0b", bg="#1e293b")
    status_label.pack(pady=(0, 8))

    code_label = tk.Label(frame, text="", font=("monospace", 24, "bold"),
                          fg="#22c55e", bg="#1e293b")
    code_label.pack(pady=(0, 16))

    button_frame = tk.Frame(frame, bg="#1e293b")
    button_frame.pack()

    def on_confirm():
        if phone_info[0] and verification_code[0]:
            info = phone_info[0]
            sym_key = crypto.derive_shared_key(info["phone_pubkey"])
            config.add_paired_device(
                device_id=info["phone_id"],
                pubkey=info["phone_pubkey"],
                symmetric_key_b64=base64.b64encode(sym_key).decode(),
                name=f"Phone-{info['phone_id'][:8]}",
            )
            api.confirm_pairing(info["phone_id"])
            paired[0] = True
            root.destroy()

    def on_cancel():
        root.destroy()

    confirm_btn = tk.Button(button_frame, text="Confirm Pairing", command=on_confirm,
                            font=("sans-serif", 11), bg="#22c55e", fg="#0f172a",
                            state=tk.DISABLED, padx=16, pady=6)
    confirm_btn.pack(side=tk.LEFT, padx=(0, 8))

    cancel_btn = tk.Button(button_frame, text="Cancel", command=on_cancel,
                           font=("sans-serif", 11), bg="#475569", fg="#f8fafc",
                           padx=16, pady=6)
    cancel_btn.pack(side=tk.LEFT)

    def poll_for_pairing():
        if not root.winfo_exists():
            return
        requests_list = api.poll_pairing()
        if requests_list:
            req = requests_list[0]
            phone_info[0] = req
            sym_key = crypto.derive_shared_key(req["phone_pubkey"])
            code = KeyManager.get_verification_code(sym_key)
            verification_code[0] = code
            status_label.config(text=f"Phone connected: {req['phone_id'][:12]}... Verify code:", fg="#22c55e")
            code_label.config(text=code)
            confirm_btn.config(state=tk.NORMAL)
        else:
            root.after(2000, poll_for_pairing)

    root.after(1000, poll_for_pairing)
    root.mainloop()

    return paired[0]


def run_pairing_headless(config: Config, crypto: KeyManager, api: ApiClient,
                          timeout: int = 120) -> bool:
    """
    Headless pairing: print QR data to terminal, poll for phone, auto-confirm.
    For testing / scripted use.
    """
    qr_data = generate_qr_data(config, crypto)
    log.info("Pairing QR data: %s", qr_data)
    log.info("Device ID: %s", crypto.get_device_id())
    log.info("Waiting for phone to pair (timeout: %ds)...", timeout)

    start = time.time()
    while time.time() - start < timeout:
        requests_list = api.poll_pairing()
        if requests_list:
            req = requests_list[0]
            sym_key = crypto.derive_shared_key(req["phone_pubkey"])
            code = KeyManager.get_verification_code(sym_key)
            log.info("Phone connected: %s. Verification code: %s", req["phone_id"][:12], code)

            config.add_paired_device(
                device_id=req["phone_id"],
                pubkey=req["phone_pubkey"],
                symmetric_key_b64=base64.b64encode(sym_key).decode(),
                name=f"Phone-{req['phone_id'][:8]}",
            )
            api.confirm_pairing(req["phone_id"])
            log.info("Pairing confirmed")
            return True
        time.sleep(2)

    log.error("Pairing timed out")
    return False
