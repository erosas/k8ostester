"""Load generator unit tests.

psycopg is stubbed by conftest (in-cluster-only dependency); everything else
runs real — the Pool, the client loops, and main() are exercised with mocks
only at the connection boundary. Ops land on stdout as JSON lines, so capsys
is the journal.
"""

import asyncio
import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from k8ostester.technologies.postgres_cnpg import loadgen


@pytest.fixture(autouse=True)
def reset_module_state():
    yield
    loadgen.DSN, loadgen.PHASES = "", []
    loadgen.WORKERS, loadgen.INDEX = 1, 0
    loadgen.known_max_id = 0


def journal(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def mock_conn(**kwargs) -> MagicMock:
    return MagicMock(close=AsyncMock(), **kwargs)


def test_record(capsys):
    loadgen.record("write", 0, time.time(), True)
    (rec,) = journal(capsys)
    assert rec["kind"] == "op"
    assert rec["op"] == "write"
    assert rec["ok"] is True
    assert rec["lat_ms"] >= 0


def test_read_env_config(monkeypatch):
    monkeypatch.setenv("K8OST_DSN", "host=db")
    monkeypatch.setenv("K8OST_PHASES", '[{"duration_s": 1}]')
    monkeypatch.setenv("K8OST_WORKERS", "4")
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "2")
    loadgen.read_env_config()
    assert (loadgen.DSN, loadgen.WORKERS, loadgen.INDEX) == ("host=db", 4, 2)
    assert loadgen.PHASES == [{"duration_s": 1}]


def test_shard_splits_clients_and_scales_rate(monkeypatch):
    monkeypatch.setattr(loadgen, "WORKERS", 3)
    phase = {"duration_s": 5, "clients": 10, "rate": 20.0}

    monkeypatch.setattr(loadgen, "INDEX", 0)
    assert loadgen.shard(phase) == dict(phase, clients=4, rate=8.0)
    monkeypatch.setattr(loadgen, "INDEX", 2)
    assert loadgen.shard(phase) == dict(phase, clients=3, rate=6.0)


def test_shard_pause_phase_unchanged(monkeypatch):
    monkeypatch.setattr(loadgen, "WORKERS", 3)
    phase = {"duration_s": 5, "clients": 0, "rate": 0}
    assert loadgen.shard(phase) == dict(phase, clients=0)


# -- connection handling -------------------------------------------------------


async def test_connect_success(monkeypatch, capsys):
    conn = mock_conn()
    monkeypatch.setattr(
        loadgen.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)
    )
    assert await loadgen.connect(0) is conn
    (rec,) = journal(capsys)
    assert rec["op"] == "connect"
    assert rec["ok"] is True


async def test_connect_failure_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(
        loadgen.psycopg.AsyncConnection, "connect",
        AsyncMock(side_effect=OSError("refused")),
    )
    assert await loadgen.connect(2) is None
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="connect", phase=2, ok=False, err="OSError")


async def test_safe_close_swallows_errors():
    conn = MagicMock(close=AsyncMock(side_effect=RuntimeError("already dead")))
    await loadgen.safe_close(conn)  # must not raise


async def test_safe_close_is_bounded(monkeypatch):
    monkeypatch.setattr(loadgen, "CLOSE_TIMEOUT", 0.02)

    async def hang():
        await asyncio.sleep(60)

    await loadgen.safe_close(MagicMock(close=hang))  # returns, doesn't hang


# -- pool ----------------------------------------------------------------------


async def test_pool_reuses_fresh_idle_connection():
    conn = mock_conn()
    pool = loadgen.Pool(size=1, phase_i=0)
    pool.idle.append((conn, time.time()))  # inside the alive-bypass window

    assert await pool.acquire() is conn
    conn.execute.assert_not_called()  # recently used: validation skipped

    pool.release(conn)
    assert len(pool.idle) == 1
    await pool.close()
    conn.close.assert_awaited_once()


async def test_pool_validates_stale_idle_connection():
    conn = mock_conn(execute=AsyncMock())
    pool = loadgen.Pool(size=1, phase_i=0)
    pool.idle.append((conn, time.time() - 60))  # stale: must be validated

    assert await pool.acquire() is conn
    conn.execute.assert_awaited_once_with("select 1")


async def test_pool_replaces_dead_idle_connection(monkeypatch):
    dead = mock_conn(execute=AsyncMock(side_effect=OSError("gone")))
    fresh = mock_conn()
    monkeypatch.setattr(loadgen, "connect", AsyncMock(return_value=fresh))
    pool = loadgen.Pool(size=1, phase_i=0)
    pool.idle.append((dead, time.time() - 60))

    assert await pool.acquire() is fresh
    dead.close.assert_awaited()


async def test_pool_acquire_bounded_when_exhausted(monkeypatch, capsys):
    """A full pool journals a failed connect (PoolTimeout) instead of hanging —
    from the app's perspective that IS a connection failure."""
    monkeypatch.setattr(loadgen, "ACQUIRE_TIMEOUT", 0.05)
    pool = loadgen.Pool(size=1, phase_i=3)
    await pool.slots.acquire()  # the only slot is held and never released

    t0 = time.monotonic()
    assert await pool.acquire() is None
    assert time.monotonic() - t0 < 1

    (rec,) = journal(capsys)
    assert rec == dict(rec, op="connect", phase=3, ok=False, err="PoolTimeout")


async def test_pool_acquire_deadline_spans_dead_idle_stack(monkeypatch, capsys):
    """After an outage the idle stack is dead connections; per-conn validation
    timeouts must not stack past ACQUIRE_TIMEOUT (the never-hang invariant)."""
    monkeypatch.setattr(loadgen, "ACQUIRE_TIMEOUT", 0.1)
    monkeypatch.setattr(loadgen, "VALIDATE_TIMEOUT", 0.06)

    async def hang(*_):
        await asyncio.sleep(60)

    pool = loadgen.Pool(size=4, phase_i=0)
    dead = [mock_conn(execute=hang) for _ in range(4)]
    pool.idle += [(c, time.time() - 60) for c in dead]

    t0 = time.monotonic()
    assert await pool.acquire() is None
    assert time.monotonic() - t0 < 1

    recs = journal(capsys)
    assert recs[-1]["err"] == "PoolTimeout"


async def test_pool_acquire_deadline_after_last_dead_conn(monkeypatch, capsys):
    """The deadline can also expire as the last dead connection is discarded —
    no fresh connect may be attempted past ACQUIRE_TIMEOUT."""
    monkeypatch.setattr(loadgen, "ACQUIRE_TIMEOUT", 0.03)
    monkeypatch.setattr(loadgen, "VALIDATE_TIMEOUT", 0.06)  # one validation eats it

    async def hang(*_):
        await asyncio.sleep(60)

    pool = loadgen.Pool(size=1, phase_i=0)
    pool.idle.append((mock_conn(execute=hang), time.time() - 60))

    assert await pool.acquire() is None
    recs = journal(capsys)
    assert recs[-1]["err"] == "PoolTimeout"


async def test_pool_acquire_returns_none_when_connect_fails(monkeypatch):
    monkeypatch.setattr(loadgen, "connect", AsyncMock(return_value=None))
    pool = loadgen.Pool(size=1, phase_i=0)

    assert await pool.acquire() is None
    assert not pool.slots.locked()  # slot released for the next attempt


async def test_pool_release_broken_discards_connection():
    conn = mock_conn()
    pool = loadgen.Pool(size=1, phase_i=0)
    await pool.slots.acquire()

    pool.release(conn, broken=True)
    for _ in range(5):
        await asyncio.sleep(0)  # let the fire-and-forget close task run

    conn.close.assert_awaited_once()
    assert pool.idle == []


# -- ops -----------------------------------------------------------------------


async def test_write_op_journals_ack_and_tracks_max_id(capsys):
    cursor = MagicMock(fetchone=AsyncMock(return_value=[123]))
    conn = MagicMock(execute=AsyncMock(return_value=cursor))

    await loadgen.write_op(conn, client_id=1, phase=0)

    assert loadgen.known_max_id == 123
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="write", ok=True, id=123)
    assert rec["checksum"]


async def test_read_op_before_any_write_is_a_ping(capsys):
    conn = MagicMock(execute=AsyncMock())
    await loadgen.read_op(conn, phase=0)
    conn.execute.assert_awaited_once_with("select 1")
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="read", ok=True)


async def test_read_op_verifies_checksum(capsys):
    payload = "hello"
    checksum = hashlib.sha256(payload.encode()).hexdigest()[:16]
    cursor = MagicMock(fetchone=AsyncMock(return_value=[payload, checksum]))
    conn = MagicMock(execute=AsyncMock(return_value=cursor))
    loadgen.known_max_id = 5

    await loadgen.read_op(conn, phase=0)
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="read", ok=True)


async def test_read_op_flags_checksum_mismatch(capsys):
    cursor = MagicMock(fetchone=AsyncMock(return_value=["payload", "bogus"]))
    conn = MagicMock(execute=AsyncMock(return_value=cursor))
    loadgen.known_max_id = 5

    await loadgen.read_op(conn, phase=0)
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="read", ok=False, err="checksum_mismatch")


def test_pick_op_respects_mix(monkeypatch):
    monkeypatch.setattr(loadgen.random, "random", lambda: 0.1)
    assert loadgen.pick_op({"mix": {"write": 0.5}}) == "write"
    assert loadgen.pick_op({"mix": {"write": 0.0}}) == "read"


async def test_do_op_success(capsys):
    cursor = MagicMock(fetchone=AsyncMock(return_value=[7]))
    conn = MagicMock(execute=AsyncMock(return_value=cursor))

    ok = await loadgen.do_op(conn, client_id=1, phase_i=0, op="write")

    assert ok is True
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="write", ok=True)


async def test_do_op_read_path(capsys):
    conn = MagicMock(execute=AsyncMock())  # known_max_id 0 → ping read

    ok = await loadgen.do_op(conn, client_id=1, phase_i=0, op="read")

    assert ok is True
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="read", ok=True)


async def test_do_op_failure_journals_error_and_flags_discard(capsys):
    conn = MagicMock(execute=AsyncMock(side_effect=RuntimeError("connection reset")))

    ok = await loadgen.do_op(conn, client_id=1, phase_i=0, op="write")

    assert ok is False
    (rec,) = journal(capsys)
    assert rec == dict(rec, op="write", ok=False, err="RuntimeError")


# -- client loops ----------------------------------------------------------------
# rate 0 → no pacing sleep; the mocked do_op advances the fake clock, so the
# deadline decides the iteration count deterministically.


async def test_pooled_client_runs_until_deadline(fake_clock, monkeypatch):
    conn = MagicMock()
    pool = MagicMock(acquire=AsyncMock(return_value=conn))
    do_op = AsyncMock(side_effect=lambda *a, **k: fake_clock.sleep(3) or True)
    monkeypatch.setattr(loadgen, "do_op", do_op)

    phase = {"clients": 1, "rate": 0, "mix": {"write": 1}}  # writes → write_pool
    await loadgen.pooled_client(pool, pool, 1, 0, phase, deadline=fake_clock.time() + 5)

    assert do_op.await_count == 2  # 3s per op in a 5s window
    pool.release.assert_called_with(conn, broken=False)


async def test_pooled_client_routes_reads_to_read_pool(fake_clock, monkeypatch):
    """writes → write pool, reads → read pool (split datasource)."""
    wconn, rconn = MagicMock(name="w"), MagicMock(name="r")
    write_pool = MagicMock(acquire=AsyncMock(return_value=wconn))
    read_pool = MagicMock(acquire=AsyncMock(return_value=rconn))
    ops = iter(["write", "read"])
    monkeypatch.setattr(loadgen, "pick_op", lambda phase: next(ops))
    monkeypatch.setattr(loadgen, "do_op",
                        AsyncMock(side_effect=lambda *a, **k: fake_clock.sleep(3) or True))

    phase = {"clients": 1, "rate": 0, "mix": {}}
    await loadgen.pooled_client(write_pool, read_pool, 1, 0, phase, deadline=fake_clock.time() + 5)

    write_pool.acquire.assert_awaited_once()   # the write op
    read_pool.acquire.assert_awaited_once()    # the read op
    write_pool.release.assert_called_once_with(wconn, broken=False)
    read_pool.release.assert_called_once_with(rconn, broken=False)


async def test_pooled_client_retries_after_pool_timeout(fake_clock, monkeypatch):
    conn = MagicMock()
    pool = MagicMock(acquire=AsyncMock(side_effect=[None, conn]))
    do_op = AsyncMock(side_effect=lambda *a, **k: fake_clock.sleep(10) or False)
    monkeypatch.setattr(loadgen, "do_op", do_op)

    phase = {"clients": 1, "rate": 0, "mix": {"write": 1}}
    await loadgen.pooled_client(pool, pool, 1, 0, phase, deadline=fake_clock.time() + 5)

    assert pool.acquire.await_count == 2  # None → retry, acquire journaled it
    pool.release.assert_called_once_with(conn, broken=True)  # do_op returned False


async def test_churn_client_one_connection_per_op(fake_clock, monkeypatch):
    conn = mock_conn()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(loadgen, "connect", connect)
    do_op = AsyncMock(side_effect=lambda *a, **k: fake_clock.sleep(3) or True)
    monkeypatch.setattr(loadgen, "do_op", do_op)

    # rate 0: no pacing asyncio.sleep — under fake_clock the loop clock is
    # frozen, so a real asyncio.sleep(>0) would never complete
    phase = {"clients": 1, "rate": 0, "mix": {"write": 1}}
    await loadgen.churn_client(1, 0, phase, deadline=fake_clock.time() + 5)

    assert connect.await_count == 2
    assert do_op.await_count == 2
    assert conn.close.await_count == 2  # closed after every op


# -- phase orchestration ---------------------------------------------------------


async def test_run_phase_one_task_per_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(loadgen, "pooled_client", client)
    phase = {"clients": 3, "rate": 5.0, "mode": "persistent", "mix": {}}

    await loadgen.run_phase(0, phase, deadline=time.time())

    assert client.await_count == 3


async def test_run_phase_churn_mode_has_no_pool(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(loadgen, "churn_client", client)
    phase = {"clients": 2, "rate": 5.0, "mode": "churn", "mix": {}}

    await loadgen.run_phase(0, phase, deadline=time.time())

    assert client.await_count == 2


async def test_run_phase_cancels_stragglers(monkeypatch, capsys):
    """One stuck client must not stall the phase plan — it is cancelled after
    the grace window and journaled."""
    monkeypatch.setattr(loadgen, "STRAGGLER_GRACE", 0.05)

    async def stuck(*a, **k):
        await asyncio.Event().wait()

    monkeypatch.setattr(loadgen, "pooled_client", stuck)
    phase = {"clients": 1, "rate": 1.0, "mode": "persistent", "mix": {}}

    await loadgen.run_phase(0, phase, deadline=time.time())  # deadline already passed

    recs = journal(capsys)
    assert any(r["kind"] == "stragglers" and r["cancelled"] == 1 for r in recs)


async def test_main_end_to_end_mini_run(monkeypatch, capsys):
    """The full pipeline with psycopg faked at the connection boundary:
    schema setup, a short persistent phase plus a pause phase, ops journaled,
    done record carrying the max acked id."""
    ids = iter(range(1, 10_000))
    cursor = MagicMock(fetchone=AsyncMock(side_effect=lambda: [next(ids)]))
    conn = mock_conn(execute=AsyncMock(return_value=cursor))
    monkeypatch.setattr(
        loadgen.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)
    )
    monkeypatch.setenv("K8OST_DSN", "host=stub")
    monkeypatch.setenv("K8OST_PHASES", json.dumps([
        {"duration_s": 0.2, "rate": 200.0, "mix": {"write": 1}, "clients": 2,
         "mode": "persistent"},
        {"duration_s": 0, "rate": 0, "mix": {}, "clients": 0, "mode": "persistent"},
    ]))
    monkeypatch.setenv("K8OST_WORKERS", "1")
    monkeypatch.delenv("JOB_COMPLETION_INDEX", raising=False)

    await loadgen.main()

    recs = journal(capsys)
    assert {"start", "phase", "done"} <= {r["kind"] for r in recs}
    writes = [r for r in recs if r["kind"] == "op" and r["op"] == "write" and r["ok"]]
    assert writes  # ops flowed
    done = next(r for r in recs if r["kind"] == "done")
    assert done["max_id"] == max(w["id"] for w in writes)
