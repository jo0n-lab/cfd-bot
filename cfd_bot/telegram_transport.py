"""urllib HTTPS transport with bounded waits and credential-free phase timings."""
from contextlib import contextmanager
from functools import lru_cache
import http.client
import ssl
import time
import urllib.request

from . import diagnostics


@contextmanager
def phase(method, stage, **fields):
    started = time.monotonic()
    error = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        diagnostics.event('telegram.transport.phase', method=method, stage=stage,
                          duration_ms=(time.monotonic()-started)*1000,
                          error_type=error, **fields)


@lru_cache(maxsize=1)
def _tls_context():
    return ssl.create_default_context()


class _Connection(http.client.HTTPSConnection):
    def __init__(self, host, *, api_method, **kwargs):
        super().__init__(host, **kwargs)
        self.api_method = api_method

    def connect(self):
        original = self.timeout
        self.timeout = min(original, 10)
        try:
            # Includes resolver, TCP, proxy CONNECT (if any), and verified TLS.
            with phase(self.api_method, 'connect'):
                super().connect()
        finally:
            self.timeout = original
        self.sock.settimeout(original)

    def send(self, data):
        if self.sock is None:
            self.connect()
        with phase(self.api_method, 'request_send', bytes=len(data)):
            return super().send(data)

    def getresponse(self):
        self.sock.settimeout(min(self.timeout, 20))
        with phase(self.api_method, 'response_headers'):
            return super().getresponse()


class _Handler(urllib.request.HTTPSHandler):
    def __init__(self, method):
        super().__init__(context=_tls_context())
        self.method = method

    def https_open(self, request):
        def connection(host, **kwargs):
            return _Connection(host, api_method=self.method, **kwargs)
        return self.do_open(connection, request, context=self._context,
                            check_hostname=self._check_hostname)


def open_request(request, *, timeout, method):
    # Per-call opener avoids mutable request state shared by polling/upload threads.
    # Default handlers retain urllib proxy support and HTTP error handling.
    return urllib.request.build_opener(_Handler(method)).open(request, timeout=timeout)
