from http.server import HTTPServer, BaseHTTPRequestHandler
import logging
import os
import socket
from socketserver import ThreadingMixIn
import ssl
import sys

# import kubernetes
# import requests


logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'DEBUG'))
log = logging.getLogger('unidler')


class RequestHandler(BaseHTTPRequestHandler):

    def do_HEAD(self):
        self.respond(200)

    def do_GET(self):
        log.debug(self.requestline)
        log.debug(self.headers)

        self.cache_request()
        self.mark_unidling()
        # self.restore_replicas()
        # self.restore_ingress()
        # self.unmark_idled()

        self.respond(200, 'OK')

    def cache_request(self):
        pass

    def mark_unidling(self):
        ingress_host = self.headers['Host']

    def respond(self, status, body=None, headers={}):
        self.send_response(status)
        for header, value in headers.items():
            self.send_header(header, value)
        if 'Content-type' not in headers:
            self.send_header('Content-type', 'text/plain')
        self.end_headers()
        if body:
            self.wfile.write(str(body).encode('utf-8'))


class UnidlerServer(ThreadingMixIn, HTTPServer):
    pass


def run(host='0.0.0.0', port=8080):
    unidler = UnidlerServer((host, port), RequestHandler)
    print(f'Unidler listening on {host}:{port}')
    unidler.serve_forever()


if __name__ == '__main__':
    run(*sys.argv[1:])
