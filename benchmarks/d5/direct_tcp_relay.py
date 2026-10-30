import os
import select
import socket
import socketserver


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(
            (os.environ["DIRECT_HOST"], 18091), timeout=10
        ) as upstream:
            upstream.settimeout(None)
            sockets = [self.request, upstream]
            while True:
                ready, _, _ = select.select(sockets, [], [], 240)
                if not ready:
                    return
                for src in ready:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is self.request else self.request).sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


Server(("0.0.0.0", 8080), Relay).serve_forever()
