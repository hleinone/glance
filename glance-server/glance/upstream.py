"""Reading an answer from a data provider (FMI, Digitransit), within limits.

urllib's timeout limits each read, not the whole answer: a server sending a
byte every few seconds could keep a fetch going for ever, and an answer of any
size would be read into memory (the Pi has little). So every read from the
connection, the status line and headers included, waits no longer than what is
left of a deadline for the whole answer, and the body is read in pieces up to
a size limit. Redirects are refused: neither provider uses them, and urllib
would send the request's headers (the Digitransit key) on to wherever one
pointed, plain HTTP included.

Not covered: connecting (with its TLS handshake) gets what is left of the
deadline once per address tried, and looking up the name is not limited at all.
"""

from __future__ import annotations

import http.client
import io
import time
import urllib.error
import urllib.request

MAX_BYTES = 2_000_000  # a forecast is ~250 KB, departures ~50 KB


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the 3xx as an HTTPError


class _DeadlineReads(io.RawIOBase):
    """A connection's reads, each waiting at most what is left until `end`."""

    def __init__(self, sock, raw, end: float, timeout: float | None):
        self.sock, self.raw, self.end, self.timeout = sock, raw, end, timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        left = self.end - time.monotonic()
        if left <= 0:
            raise TimeoutError("answer not complete in time")
        self.sock.settimeout(left if self.timeout is None else min(self.timeout, left))
        return self.raw.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self.raw.close()
        super().close()


class _DeadlineSocket:
    """The connection's socket, whose reader keeps to the deadline."""

    def __init__(self, sock, end: float, timeout: float | None):
        self._sock, self._end, self._timeout = sock, end, timeout

    def makefile(self, mode: str = "r", *args, **kwargs):
        # The socket's own reader, for its reference counting: the socket
        # stays open while the answer is read, after urllib lets it go.
        raw = self._sock.makefile("rb", buffering=0)
        return io.BufferedReader(_DeadlineReads(self._sock, raw, self._end, self._timeout))

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _with_deadline(connection: type[http.client.HTTPConnection], end: float) -> type[http.client.HTTPConnection]:
    class Connection(connection):
        def connect(self) -> None:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("answer not complete in time")
            per_step = self.timeout if isinstance(self.timeout, (int, float)) else None
            self.timeout = left if per_step is None else min(per_step, left)
            super().connect()
            self.sock = _DeadlineSocket(self.sock, end, per_step)

    return Connection


class _Deadlines(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Opens connections that keep to the request's `deadline_end`."""

    def __init__(self, context=None):
        urllib.request.HTTPHandler.__init__(self)
        urllib.request.HTTPSHandler.__init__(self, context=context)

    def http_open(self, req):
        return self.do_open(_with_deadline(http.client.HTTPConnection, req.deadline_end), req)

    def https_open(self, req):
        return self.do_open(_with_deadline(http.client.HTTPSConnection, req.deadline_end), req,
                            context=self._context)


def _handlers(context=None) -> tuple[urllib.request.BaseHandler, ...]:
    """What every provider's answer is read through: no redirects, the deadline."""
    return _NoRedirects(), _Deadlines(context)


_opener = urllib.request.build_opener(*_handlers())


def _open(req: urllib.request.Request, timeout: float):
    """The tests' seam: they replace this instead of reaching the network."""
    return _opener.open(req, timeout=timeout)


def _drain(resp, end: float, max_bytes: int) -> bytes:
    chunks, size = [], 0
    while chunk := resp.read1(65536):  # one read from the socket at most
        size += len(chunk)
        if size > max_bytes:
            raise OSError(f"answer larger than {max_bytes} bytes")
        if time.monotonic() > end:
            raise TimeoutError("answer not complete in time")
        chunks.append(chunk)
    if getattr(resp, "length", None):  # the connection ended before Content-Length was reached
        raise http.client.IncompleteRead(b"".join(chunks), resp.length)
    return b"".join(chunks)


def read(req: urllib.request.Request, timeout: float, deadline: float, max_bytes: int = MAX_BYTES) -> bytes:
    """The body of the answer to `req`.

    timeout: seconds for each step (connecting, each read); deadline: seconds
    for reading the whole answer, headers included, counted from the start of
    the request. Raises what urllib does (HTTPError, other OSErrors,
    http.client.HTTPException), plus TimeoutError and OSError for the two
    limits.
    """
    end = time.monotonic() + deadline
    req.deadline_end = end
    with _open(req, timeout) as resp:
        return _drain(resp, end, max_bytes)


def read_error(error: urllib.error.HTTPError, deadline: float, max_bytes: int = MAX_BYTES) -> bytes:
    """The body that came with an HTTP error status, within the same limits."""
    if error.fp is None:
        return b""
    return _drain(error.fp, time.monotonic() + deadline, max_bytes)
