"""K8osTester Postgres load generator.

Runs in-cluster as a Job (stock python image + this script via ConfigMap).
Executes a pre-declared phase plan and emits one JSON line per operation to
stdout — that stream IS the journal: acked writes (ok inserts with id +
checksum) are later reconciled against the database for RPO/integrity, and
every record carries timestamps/latencies for goal evaluation.

Client model (mode "persistent"): the phase's clients share a HikariCP-style
local connection pool — fixed size, validate-on-checkout (skipped inside a
500ms alive-bypass window, like Hikari), broken connections replaced, low
acquisition timeout. That is how real applications sit in front of a database
or PgBouncer, and it keeps the instrument honest: no operation can hang past
its timeout, so an outage shows up as failed/absent ops — never as the
loadgen itself stalling (which once graphed as a phantom 150s outage).
Mode "churn" stays one raw connect per op: it models pool-less clients and
is what the direct-vs-pooler comparison experiments measure.

"connect" op records keep one meaning: real connection establishment.
A pool-acquire timeout journals as a failed connect (err=PoolTimeout) —
from the app's perspective that IS a connection failure.

Env:
  K8OST_DSN     postgres connection string
  K8OST_PHASES  JSON: [{"duration_s": 60, "rate": 20.0, "mix": {"read": 0.5,
                "write": 0.5}, "clients": 5, "mode": "persistent"}, ...]
                rate 0/null with no clients → pause phase.
"""

import asyncio
import hashlib
import json
import os
import random
import string
import sys
import time

import psycopg

DSN = os.environ["K8OST_DSN"]
PHASES = json.loads(os.environ["K8OST_PHASES"])
# worker pool sharding (Indexed Job): this pod runs its slice of the global
# client count, with rate scaled to its share so per-client pacing is unchanged
WORKERS = int(os.environ.get("K8OST_WORKERS", "1"))
INDEX = int(os.environ.get("JOB_COMPLETION_INDEX") or 0)

CONNECT_TIMEOUT = 3  # s — TCP+auth for one connection attempt
ACQUIRE_TIMEOUT = 5  # s — Hikari connectionTimeout (default 30s, tightened)
OP_TIMEOUT = 10  # s — statement bound; a hung query is a failed op, not a hang
VALIDATE_TIMEOUT = 2  # s — checkout validation query
ALIVE_BYPASS = 0.5  # s — Hikari aliveBypassWindow: recently-used conns skip validation
CLOSE_TIMEOUT = 2  # s — closing a broken conn must not block teardown

SCHEMA = """
create table if not exists k8ost_ops (
    id bigserial primary key,
    client int not null,
    phase int not null,
    payload text not null,
    checksum text not null,
    created timestamptz not null default now()
)
"""

known_max_id = 0


def out(**rec):
    print(json.dumps(rec), flush=True)


def record(op, phase, t0, ok, err=None, **extra):
    out(
        kind="op", op=op, phase=phase, t=t0, lat_ms=round((time.time() - t0) * 1000, 2),
        ok=ok, **({"err": err} if err else {}), **extra,
    )


async def safe_close(conn):
    try:
        await asyncio.wait_for(conn.close(), CLOSE_TIMEOUT)
    except Exception:
        pass


async def connect(phase):
    t0 = time.time()
    try:
        # prepare_threshold=None: server-side prepared statements break
        # PgBouncer transaction pooling; disabled everywhere for comparability
        conn = await psycopg.AsyncConnection.connect(
            DSN, autocommit=True, connect_timeout=CONNECT_TIMEOUT, prepare_threshold=None
        )
        record("connect", phase, t0, True)
        return conn
    except Exception as e:
        record("connect", phase, t0, False, err=type(e).__name__)
        return None


class Pool:
    """Hikari-like fixed-size pool: one permit per live connection slot,
    idle stack, validate-on-checkout outside the alive-bypass window."""

    def __init__(self, size, phase_i):
        self.phase_i = phase_i
        self.idle = []  # [(conn, last_used)]
        self.slots = asyncio.Semaphore(size)

    async def acquire(self):
        """Bounded end-to-end by ACQUIRE_TIMEOUT — after an outage the idle
        stack is full of dead connections and each failed validation costs up
        to VALIDATE_TIMEOUT; without a total deadline one acquire could chew
        through dozens of them and stall a client far past the phase end."""
        t0 = time.time()
        deadline = t0 + ACQUIRE_TIMEOUT
        try:
            await asyncio.wait_for(self.slots.acquire(), ACQUIRE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            record("connect", self.phase_i, t0, False, err="PoolTimeout")
            return None
        while self.idle:
            if time.time() >= deadline:
                self.slots.release()
                record("connect", self.phase_i, t0, False, err="PoolTimeout")
                return None
            conn, last_used = self.idle.pop()
            if time.time() - last_used < ALIVE_BYPASS or await self._valid(conn):
                return conn
            await safe_close(conn)
        if time.time() >= deadline:
            self.slots.release()
            record("connect", self.phase_i, t0, False, err="PoolTimeout")
            return None
        conn = await connect(self.phase_i)  # journals the real connect
        if conn is None:
            self.slots.release()
        return conn

    def release(self, conn, broken=False):
        if broken:
            asyncio.ensure_future(safe_close(conn))
        else:
            self.idle.append((conn, time.time()))
        self.slots.release()

    async def _valid(self, conn):
        try:
            await asyncio.wait_for(conn.execute("select 1"), VALIDATE_TIMEOUT)
            return True
        except Exception:
            return False

    async def close(self):
        while self.idle:
            conn, _ = self.idle.pop()
            await safe_close(conn)


async def write_op(conn, client_id, phase):
    global known_max_id
    payload = "".join(random.choices(string.ascii_letters, k=64))
    checksum = hashlib.sha256(payload.encode()).hexdigest()[:16]
    t0 = time.time()
    cur = await conn.execute(
        "insert into k8ost_ops (client, phase, payload, checksum) values (%s, %s, %s, %s) returning id",
        (client_id, phase, payload, checksum),
    )
    row = await cur.fetchone()
    known_max_id = max(known_max_id, row[0])
    record("write", phase, t0, True, id=row[0], checksum=checksum)


async def read_op(conn, phase):
    target = random.randint(1, known_max_id) if known_max_id else None
    t0 = time.time()
    if target is None:
        await conn.execute("select 1")
        record("read", phase, t0, True)
        return
    cur = await conn.execute(
        "select payload, checksum from k8ost_ops where id = %s", (target,)
    )
    row = await cur.fetchone()
    ok = row is None or hashlib.sha256(row[0].encode()).hexdigest()[:16] == row[1]
    record("read", phase, t0, ok, err=None if ok else "checksum_mismatch")


async def do_op(conn, client_id, phase_i, phase):
    """One read/write under the statement bound; returns False if the
    connection must be discarded."""
    op = "write" if random.random() < phase["mix"].get("write", 0.5) else "read"
    t0 = time.time()
    try:
        if op == "write":
            await asyncio.wait_for(write_op(conn, client_id, phase_i), OP_TIMEOUT)
        else:
            await asyncio.wait_for(read_op(conn, phase_i), OP_TIMEOUT)
        return True
    except Exception as e:
        record(op, phase_i, t0, False, err=type(e).__name__)
        return False


async def pooled_client(pool, client_id, phase_i, phase, deadline):
    interval = phase["clients"] / phase["rate"] if phase["rate"] else None
    while time.time() < deadline:
        if interval:
            # aggregate ≈ rate: each client sleeps clients/rate with jitter
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))
        conn = await pool.acquire()
        if conn is None:
            continue  # acquire journaled the failure; pool refills on next try
        ok = await do_op(conn, client_id, phase_i, phase)
        pool.release(conn, broken=not ok)


async def churn_client(client_id, phase_i, phase, deadline):
    interval = phase["clients"] / phase["rate"] if phase["rate"] else None
    while time.time() < deadline:
        if interval:
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))
        conn = await connect(phase_i)
        if conn is None:
            await asyncio.sleep(1)
            continue
        await do_op(conn, client_id, phase_i, phase)
        await safe_close(conn)


def shard(phase):
    """This worker's slice: clients split as evenly as possible, rate scaled
    to the share so interval = clients/rate is identical on every pod."""
    total = phase["clients"]
    local = total // WORKERS + (1 if INDEX < total % WORKERS else 0)
    sharded = dict(phase, clients=local)
    if phase["rate"] and total:
        sharded["rate"] = phase["rate"] * local / total
    return sharded


async def run_phase(i, phase, deadline):
    pool = Pool(phase["clients"], i) if phase["mode"] == "persistent" else None
    tasks = [
        asyncio.create_task(
            pooled_client(pool, INDEX * 100000 + c, i, phase, deadline)
            if pool
            else churn_client(INDEX * 100000 + c, i, phase, deadline)
        )
        for c in range(phase["clients"])
    ]
    # bounded: one straggler must not stall the phase plan (the instrument
    # once sat 150s in gather() and graphed it as a database outage)
    grace = ACQUIRE_TIMEOUT + OP_TIMEOUT + 5
    done, pending = await asyncio.wait(tasks, timeout=max(deadline - time.time(), 0) + grace)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        out(kind="stragglers", phase=i, cancelled=len(pending), t=time.time())
    if pool:
        await pool.close()


async def main():
    conn = None
    for attempt in range(30):
        conn = await connect(-1)
        if conn:
            break
        await asyncio.sleep(2)
    if conn is None:
        out(kind="fatal", msg="database unreachable")
        sys.exit(1)
    await conn.execute(SCHEMA)
    await conn.close()
    out(kind="start", t=time.time(), phases=len(PHASES))

    # phases start at their SCHEDULED offsets, not when the previous phase's
    # teardown finishes: a straggler stuck in cancellation (e.g. psycopg trying
    # to cancel a query on a partitioned server) must wind down concurrently
    # with the next phase's traffic, or the demand gap graphs as a fake outage
    t_start = time.time()
    offset, phase_tasks = 0.0, []
    for i, phase in enumerate(PHASES):
        phase_tasks.append(
            asyncio.create_task(run_phase_at(t_start + offset, i, phase))
        )
        offset += phase["duration_s"]
    await asyncio.gather(*phase_tasks)

    out(kind="done", t=time.time(), max_id=known_max_id)


async def run_phase_at(start_at, i, phase):
    delay = start_at - time.time()
    if delay > 0:
        await asyncio.sleep(delay)
    local = shard(phase)
    if INDEX == 0:  # one pod narrates the (global) plan
        out(kind="phase", phase=i, t=time.time(), **phase)
    if not local["rate"] or not local["clients"]:
        await asyncio.sleep(local["duration_s"])  # pause phase / no slice
        return
    await run_phase(i, local, start_at + local["duration_s"])


if __name__ == "__main__":
    asyncio.run(main())