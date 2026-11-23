# backoff-headers

Turn `Retry-After` and rate-limit headers into a number of seconds to wait.

[![PyPI](https://img.shields.io/pypi/v/backoff-headers.svg)](https://pypi.org/project/backoff-headers/)
[![Python](https://img.shields.io/pypi/pyversions/backoff-headers.svg)](https://pypi.org/project/backoff-headers/)
[![tests](https://github.com/sandeshbagmare/backoff-headers/actions/workflows/ci.yml/badge.svg)](https://github.com/sandeshbagmare/backoff-headers/actions/workflows/ci.yml)
[![license](https://img.shields.io/pypi/l/backoff-headers.svg)](https://github.com/sandeshbagmare/backoff-headers/blob/main/LICENSE)

```python
from backoff_headers import wait_time

response = client.get("/search")
seconds = wait_time(response.headers)
if seconds is not None:
    time.sleep(seconds)
```

Two headers describe the same thing and disagree about units:

| header | meaning | trap |
| ------ | ------- | ---- |
| `Retry-After` | seconds **or** an HTTP date | parsing the date as a number gives a wait of eleven days |
| `RateLimit-Reset` (RFC 9331) | seconds **from now** | reading it as a timestamp waits until 1970 |
| `X-RateLimit-Reset` (GitHub) | an absolute **Unix timestamp** | reading it as a delta waits for weeks |

`wait_time` reads whichever is present, resolves the unit, and returns seconds — or
`None` when there is no reason to wait at all.

## Install

```console
pip install backoff-headers
```

## API

```python
from backoff_headers import (
    format_retry_after, parse_rate_limit, parse_retry_after, wait_time,
)

parse_retry_after("120")                            # 120.0
parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT")  # seconds until then, 0.0 if past

limit = parse_rate_limit(response.headers)
limit.limit, limit.remaining, limit.window, limit.policy
limit.exhausted                  # remaining == 0
limit.seconds_until_reset()

wait_time(headers, cap=60, jitter=2.0)   # never longer than 60s, spread by up to 2s
format_retry_after(1.2)                  # "2", rounded up so you never retry early
```

`reset_style` on `parse_rate_limit` is `"auto"` (an implausibly large reset is treated
as a timestamp), `"delta"` or `"epoch"` when you know what your server sends. `now` is
accepted everywhere a clock is needed, which is what makes the behaviour testable.

Both header families are read case-insensitively, and RFC 9331 wins when a server
sends both.

## Design notes

- Precedence: `Retry-After`, then an exhausted quota's reset, then `default`.
- `jitter` adds `uniform(0, jitter)`, so a fleet of clients does not retry in lockstep.
- `cap` is applied after the jitter, so a cap is never exceeded.
- Nothing sleeps, nothing retries: the caller owns the loop and the clock.

## Development

```console
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

## License

MIT.
