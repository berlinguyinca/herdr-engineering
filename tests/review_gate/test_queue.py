import threading

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.models import PrKey
from herdr_engineering.review_gate.queue import ReviewQueue

K1 = PrKey.parse("github.com/acme/widgets#1")
K2 = PrKey.parse("github.com/acme/widgets#2")
K3 = PrKey.parse("github.com/acme/widgets#3")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    return ReviewQueue(ControlPlaneRepo(), now=clock, **kw), clock


def test_enqueue_is_idempotent_per_sha():
    q, _ = make()
    job = q.enqueue(K1, "a" * 40, "m1")
    assert job is not None and job.key == K1 and job.sha == "a" * 40
    assert q.enqueue(K1, "a" * 40, "m1") is None
    claimed = q.claim("w", "m2")
    assert claimed is not None
    assert q.enqueue(K1, "a" * 40, "m1") is None  # claimed
    assert q.complete(claimed.job_id, "w")
    assert q.enqueue(K1, "a" * 40, "m1") is None  # done


def test_new_sha_cancels_old_job():
    q, _ = make()
    old = q.enqueue(K1, "a" * 40, None)
    new = q.enqueue(K1, "b" * 40, None)
    assert new is not None
    assert [j["sha"] for j in q.snapshot()] == ["b" * 40]
    assert q.depth() == 1
    got = q.claim("w", "m")
    assert got.job_id == new.job_id
    assert old.job_id != new.job_id


def test_claim_is_fifo():
    q, _ = make()
    j1 = q.enqueue(K1, "a" * 40, None)
    j2 = q.enqueue(K2, "b" * 40, None)
    assert q.claim("w1", "m").job_id == j1.job_id
    assert q.claim("w2", "m").job_id == j2.job_id
    assert q.claim("w3", "m") is None


def test_claim_skips_job_authored_by_same_model():
    q, _ = make()
    q.enqueue(K1, "a" * 40, "gpt")
    j2 = q.enqueue(K2, "b" * 40, "claude")
    assert q.claim("w", "gpt").job_id == j2.job_id
    assert q.claim("w", "gpt") is None  # K1 stays queued for someone else
    assert q.depth() == 1
    assert q.claim("w2", "claude").key == K1


def test_expired_lease_requeues_once():
    q, clock = make(lease_seconds=900)
    q.enqueue(K1, "a" * 40, None)
    first = q.claim("dead", "m")
    assert q.claim("other", "m") is None  # lease still live
    clock.t += 901
    again = q.claim("w2", "m")
    assert again is not None and again.job_id == first.job_id
    assert q.claim("w3", "m") is None
    assert q.complete(first.job_id, "dead") is False
    assert q.complete(first.job_id, "w2") is True


def test_complete_by_wrong_worker_rejected():
    q, _ = make()
    q.enqueue(K1, "a" * 40, None)
    job = q.claim("w1", "m")
    assert q.complete(job.job_id, "w2") is False
    assert q.complete("nope", "w1") is False
    assert q.complete(job.job_id, "w1") is True
    assert q.complete(job.job_id, "w1") is False


def test_depth_counts_only_unclaimed():
    q, clock = make()
    q.enqueue(K1, "a" * 40, None)
    q.enqueue(K2, "b" * 40, None)
    assert q.depth() == 2
    q.claim("w", "m")
    assert q.depth() == 1
    clock.t += 10_000  # expired lease is claimable again
    assert q.depth() == 2


def test_concurrent_claims_never_double_assign():
    q, _ = make()
    for k, s in ((K1, "a"), (K2, "b"), (K3, "c")):
        q.enqueue(k, s * 40, None)
    got, lock = [], threading.Lock()
    barrier = threading.Barrier(8)

    def run(i):
        barrier.wait()
        job = q.claim(f"w{i}", "m")
        if job:
            with lock:
                got.append(job.job_id)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(got) == 3 and len(set(got)) == 3


def test_cancel_revokes_claimed_lease_and_counts():
    q, _ = make()
    q.enqueue(K1, "a" * 40, None)
    job = q.claim("w", "m")
    assert q.cancel(K1) == 1
    assert q.complete(job.job_id, "w") is False
    assert q.cancel(K1) == 0
    assert q.snapshot() == []


def test_cancel_except_sha_and_snapshot_fields():
    q, _ = make()
    q.enqueue(K1, "a" * 40, "m1")
    assert q.cancel(K1, except_sha="a" * 40) == 0
    q.claim("w", "m2")
    (row,) = q.snapshot()
    assert row["state"] == "claimed" and row["claimed_by"] == "w"
    assert row["lease_expires_at"] is not None
