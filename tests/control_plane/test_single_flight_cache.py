import threading
import time

import pytest

from herdr_engineering.control_plane.single_flight_cache import SingleFlightCache


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_a_second_caller_inside_the_ttl_gets_the_first_result_without_a_build():
    clock, calls = Clock(), []
    cache = SingleFlightCache(10, clock=clock)
    assert cache.get("k", lambda: calls.append(1) or "v1") == "v1"
    clock.t = 9.9
    assert cache.get("k", lambda: calls.append(1) or "v2") == "v1" and len(calls) == 1
    clock.t = 10.1
    assert cache.get("k", lambda: calls.append(1) or "v2") == "v2" and len(calls) == 2


def test_concurrent_callers_share_one_build():
    cache, calls, gate = SingleFlightCache(60), [], threading.Event()

    def slow():
        calls.append(1)
        gate.wait(5)
        return "built"
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get("k", slow))) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert results == ["built"] * 6 and len(calls) == 1


def test_a_failed_build_is_not_cached_and_does_not_block_the_next_caller():
    cache = SingleFlightCache(60)

    def boom():
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        cache.get("k", boom)
    assert cache.get("k", lambda: "ok") == "ok"


def test_keys_are_separate_and_the_cache_is_bounded():
    cache = SingleFlightCache(60, max_entries=2)
    assert cache.get("a", lambda: 1) == 1 and cache.get("b", lambda: 2) == 2
    cache.get("c", lambda: 3)
    assert len(cache._entries) == 2


def test_a_zero_ttl_disables_caching():
    cache, calls = SingleFlightCache(0), []
    cache.get("k", lambda: calls.append(1))
    cache.get("k", lambda: calls.append(1))
    assert len(calls) == 2
