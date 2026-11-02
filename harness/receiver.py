"""Webhook receiver: appends every POST body the Relayer sends to WEBHOOK_LOG as one JSON line."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("WEBHOOK_LOG", "/work/logs/webhooks.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), "path": self.path, "body": body}) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
