"""Shows a QR code in the terminal, for linking Telegram or WhatsApp from your phone."""

import sys

import qrcode


def show_qr(data: str, instructions: str) -> None:
    code = qrcode.QRCode(border=2)
    code.add_data(data)
    print(f"\n{instructions}\n")
    if sys.stdout.isatty():
        code.print_ascii(tty=True)  # black on white, whatever the terminal's colors
    else:
        code.print_ascii()
    print("\nThe code refreshes every 20–30 seconds.", flush=True)
