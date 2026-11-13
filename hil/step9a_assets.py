"""Owned fixture-only asset origin, plus bounded HTTP failure endpoints."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import threading
import time

ROOT=Path(os.environ.get('STEP9A_FIXTURES',Path(__file__).resolve().parent/'fixtures/step9a'))

@contextmanager
def asset_server(port=7874):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT),**kwargs)
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path.startswith('/failure/'):
                name=self.path.split('/')[-1]
                data=b'not-an-image'
                self.send_response(200)
                if name!='no-length':self.send_header('Content-Length','307200' if name in ('slow','timeout') else str(len(data)))
                self.end_headers()
                try:
                    if name=='timeout':time.sleep(7);return
                    if name=='slow':
                        for _ in range(75):self.wfile.write(b'x'*4096);self.wfile.flush();time.sleep(.2)
                    else:self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
            else:super().do_GET()
    server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(timeout=3)
