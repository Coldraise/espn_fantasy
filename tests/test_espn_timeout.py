"""espn-api's unbounded requests get a deadline.

The regression: espn-api calls the module-level `requests.get` with no timeout,
so a stalled connection hangs the thread forever. The poller is one APScheduler
job with max_instances=1, so a single hung fetch skips every later tick while
/health keeps answering 200 -- the scoreboard freezes and nothing looks broken.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import espn_client


class _Recorder:
    """Stands in for the `requests` module and records what it was handed."""

    def __init__(self):
        self.calls = []
        self.sentinel = object()

    def get(self, *args, **kwargs):
        self.calls.append(kwargs)
        return "get"

    def post(self, *args, **kwargs):
        self.calls.append(kwargs)
        return "post"


def test_get_is_given_a_default_timeout():
    inner = _Recorder()
    proxy = espn_client._TimeoutRequests(inner, 25)
    proxy.get("https://example.test")
    assert inner.calls[0]["timeout"] == 25


def test_post_is_given_a_default_timeout():
    inner = _Recorder()
    proxy = espn_client._TimeoutRequests(inner, 25)
    proxy.post("https://example.test")
    assert inner.calls[0]["timeout"] == 25


def test_an_explicit_timeout_is_not_overridden():
    inner = _Recorder()
    proxy = espn_client._TimeoutRequests(inner, 25)
    proxy.get("https://example.test", timeout=3)
    assert inner.calls[0]["timeout"] == 3


def test_other_attributes_pass_through():
    inner = _Recorder()
    proxy = espn_client._TimeoutRequests(inner, 25)
    assert proxy.sentinel is inner.sentinel


def test_the_installed_library_is_patched():
    """The real espn-api module, not just the shim in isolation."""
    from espn_api.requests import espn_requests

    assert isinstance(espn_requests.requests, espn_client._TimeoutRequests)


def test_patching_twice_does_not_nest():
    from espn_api.requests import espn_requests

    espn_client._patch_espn_api_timeouts()
    first = espn_requests.requests
    espn_client._patch_espn_api_timeouts()
    assert espn_requests.requests is first
