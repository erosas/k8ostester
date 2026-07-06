"""K8osTester Postgres load generator.

Runs in-cluster as a Job (stock python image + this script via ConfigMap).
Executes a pre-declared phase plan and emits one JSON line per operation to
stdout — that stream IS the journal: acked writes (ok inserts with id +
checksum) are later reconciled against the database for RPO/integrity, and
every record carries timestamps/latencies for goal evaluation.

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


async def connect(phase):
    t0 = time.time()
    try:
        # prepare_threshold=None: server-side prepared statements break
        # PgBouncer transaction pooling; disabled everywhere for comparability
        conn = await psycopg.AsyncConnection.connect(
            DSN, autocommit=True, connect_timeout=5, prepare_threshold=None
        )
        record("connect", phase, t0, True)
        return conn
    except Exception as e:
        record("connect", phase, t0, False, err=type(e).__name__)
        return None


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


async def client_task(client_id, phase_i, phase, deadline):
    conn = None
    interval = phase["clients"] / phase["rate"] if phase["rate"] else None
    try:
        while time.time() < deadline:
            if interval:
                # aggregate ≈ rate: each client sleeps clients/rate with jitter
                await asyncio.sleep(interval * random.uniform(0.5, 1.5))
            if conn is None or phase["mode"] == "churn":
                if conn is not None:
                    await conn.close()
                conn = await connect(phase_i)
                if conn is None:
                    await asyncio.sleep(1)
                    continue
            try:
                if random.random() < phase["mix"].get("write", 0.5):
                    await write_op(conn, client_id, phase_i)
                else:
                    await read_op(conn, phase_i)
            except Exception as e:
                record("write", phase_i, time.time(), False, err=type(e).__name__)
                try:
                    await conn.close()
                except Exception:
                    pass
                conn = None  # reconnect on next iteration
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


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

    for i, phase in enumerate(PHASES):
        out(kind="phase", phase=i, t=time.time(), **phase)
        deadline = time.time() + phase["duration_s"]
        if not phase["rate"] or not phase["clients"]:
            await asyncio.sleep(phase["duration_s"])  # pause phase
            continue
        await asyncio.gather(
            *(client_task(c, i, phase, deadline) for c in range(phase["clients"]))
        )

    out(kind="done", t=time.time(), max_id=known_max_id)


if __name__ == "__main__":
    asyncio.run(main())
