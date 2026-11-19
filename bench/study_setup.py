"""Local browser QR handoff for Study channel setup; no codes leave this Mac."""
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import secrets
import subprocess
import threading
import time

import qrcode
from qrcode.image.svg import SvgPathImage


@contextmanager
def qr_window(channel, instructions):
    token = secrets.token_urlsafe(24)
    state = {'svg': '', 'finished': False, 'opened': False, 'failed': False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/' + token or self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                self.send_error(404)
                return
            finished = state['finished']
            title = (f'{channel} setup interrupted' if state['failed'] else f'{channel} connected') if finished else f'Connect {channel}'
            message = ('Return to Study and click Open channel setup to retry.' if state['failed'] else 'Return to Study and click Test connection. You can close this tab.') if finished else instructions
            body = ('<!doctype html><html><head><meta charset="utf-8">' +
                    ('' if finished else '<meta http-equiv="refresh" content="2">') +
                    '<meta name="referrer" content="no-referrer"><title>' + escape(title) + '</title>' +
                    '<style>body{font:20px system-ui;max-width:640px;margin:48px auto;padding:24px;color:#203126}'
                    'svg{display:block;width:320px;height:320px;background:white;margin:24px auto}'
                    '</style></head><body><h1>' + escape(title) + '</h1><p>' + escape(message) + '</p>' +
                    ('' if finished else state['svg']) +
                    '<p>This page and its QR code are local to this Mac.</p></body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # QR URLs must not appear in logs.

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/{token}'

    def show(code):
        out = BytesIO()
        qrcode.make(code, image_factory=SvgPathImage, border=4).save(out)
        state['svg'] = out.getvalue().decode()
        if not state['opened']:
            subprocess.run(['open', url], check=True)
            state['opened'] = True

    try:
        yield show
    except BaseException:
        state['failed'] = True
        raise
    finally:
        state['finished'] = True
        if state['opened']:
            time.sleep(2.5)  # Let the open page load its final status.
        server.shutdown()
        server.server_close()
        thread.join()
