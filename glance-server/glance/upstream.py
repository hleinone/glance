"""Reading an answer from a data provider (FMI, Digitransit), within limits.

urllib's timeout limits each read, not the whole answer: a server sending a
byte every few seconds could keep a fetch going for ever, and an answer of any
size would be read into memory (the Pi has little). So the body is read in
pieces, with a deadline and a size limit. Redirects are refused: neither
provider uses them, and urllib would send the request's headers (the
Digitransit key) on to wherever one pointed, plain HTTP included.

Not covered: the deadline is checked between reads of the body, so a server
dripping its *headers* is still limited only per read.
"""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request

MAX_BYTES = 2_000_000  # a forecast is ~250 KB, departures ~50 KB


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the 3xx as an HTTPError


_opener = urllib.request.build_opener(_NoRedirects)


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
    for the whole body, counted from the start of the request. Raises what
    urllib does (HTTPError, other OSErrors, http.client.HTTPException), plus
    TimeoutError and OSError for the two limits.
    """
    end = time.monotonic() + deadline
    with _open(req, timeout) as resp:
        return _drain(resp, end, max_bytes)


def read_error(error: urllib.error.HTTPError, deadline: float, max_bytes: int = MAX_BYTES) -> bytes:
    """The body that came with an HTTP error status, within the same limits."""
    if error.fp is None:
        return b""
    return _drain(error.fp, time.monotonic() + deadline, max_bytes)
