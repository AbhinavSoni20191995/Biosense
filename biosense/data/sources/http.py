"""One way to fetch from a public service: bounded, and patient with throttling.

A run fetched several GEO series in one burst and NCBI answered some of them
with HTTP 429. Each of those was read as an absence — no symbol table, no
search hits — and two of the five things the run then asked a person for were
things NCBI would have served a few seconds later. A 429 is the service saying
"not now", not "not here".

So a request is retried on throttling, a server error and a timeout, with
backoff, honouring a `Retry-After` the server sends (capped, so one header
cannot stall a run). Anything else — a 404, a refused request — is the answer
and is returned at once. Network only through here, so the tests replace this
one function and nothing in ordinary CI touches a service.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

from ... import contracts as K

USER_AGENT = 'BioSenseBioinformatics/0.1'
RETRY_CODES = frozenset((429, 500, 502, 503, 504))
TRIES = 4
BACKOFF_S = (2, 4, 8)
MAX_RETRY_AFTER_S = 30
DEFAULT_MAX_BYTES = 150 * 1024 * 1024

# Replaced in tests; a run waits for real.
_sleep = time.sleep


def _retry_after(err):
    try:
        v = float((err.headers or {}).get('Retry-After') or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    return min(v, MAX_RETRY_AFTER_S) if v > 0 else None


def with_retry(fn, *, tries=TRIES):
    """fn(), retried with backoff on throttling, a server error or a timeout."""
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or i == tries - 1:
                raise
            wait = _retry_after(e) or BACKOFF_S[min(i, len(BACKOFF_S) - 1)]
        except (urllib.error.URLError, TimeoutError) as e:
            # A refused connection is not going to change in eight seconds; a
            # timeout or a reset might.
            reason = getattr(e, 'reason', e)
            transient = isinstance(reason, (TimeoutError, ConnectionResetError)) or \
                'timed out' in str(reason).lower()
            if not transient or i == tries - 1:
                raise
            wait = BACKOFF_S[min(i, len(BACKOFF_S) - 1)]
        _sleep(wait)
    raise AssertionError('unreachable')  # pragma: no cover


def get(url, timeout=60, max_bytes=DEFAULT_MAX_BYTES, data=None, headers=None):
    """Bytes at *url* (a POST when *data* is given), retried as above."""
    hdrs = {'User-Agent': USER_AGENT, **(headers or {})}

    def once():
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method='POST' if data is not None else 'GET')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise K.ContractError(f'{url} is larger than {max_bytes // 1024 // 1024} MB; '
                                  f'not fetched')
        return body
    return with_retry(once)
