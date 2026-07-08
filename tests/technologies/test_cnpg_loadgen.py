import pytest
import sys
import os
from unittest.mock import MagicMock, patch, ANY
import asyncio
import time

# Mock environment before importing loadgen
os.environ["K8OST_DSN"] = "host=localhost"
os.environ["K8OST_PHASES"] = "[]"
os.environ["K8OST_WORKERS"] = "1"

# Mock psycopg before importing loadgen
mock_psycopg = MagicMock()
mock_psycopg.AsyncConnection.connect = MagicMock()
sys.modules["psycopg"] = mock_psycopg
sys.modules["psycopg.rows"] = MagicMock()

import json
from k8ostester.technologies.postgres_cnpg import loadgen

@pytest.mark.asyncio
async def test_loadgen_record(capsys):
    loadgen.record("write", 0, time.time(), True)
    captured = capsys.readouterr()
    rec = json.loads(captured.out)
    assert rec["kind"] == "op"
    assert rec["op"] == "write"

@pytest.mark.asyncio
async def test_loadgen_pool_acquire_release():
    mock_conn = MagicMock()
    mock_conn.close = MagicMock(return_value=asyncio.Future())
    mock_conn.close.return_value.set_result(None)
    
    # Mock checkout validation
    mock_cursor = MagicMock()
    mock_cursor.execute = MagicMock(return_value=asyncio.Future())
    mock_cursor.execute.return_value.set_result(None)
    mock_conn.execute = MagicMock(return_value=asyncio.Future())
    mock_conn.execute.return_value.set_result(None)

    pool = loadgen.Pool(size=1, phase_i=0)
    pool.idle.append((mock_conn, time.time()))
    
    # We must mock _valid to return True
    with patch.object(loadgen.Pool, "_valid", return_value=asyncio.Future()) as mock_v:
        mock_v.return_value.set_result(True)
        c1 = await pool.acquire()
        assert c1 == mock_conn
    
    pool.release(c1)
    assert len(pool.idle) == 1
    
    await pool.close()
    mock_conn.close.assert_called()

@pytest.mark.asyncio
async def test_loadgen_write_op():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__aenter__ = MagicMock(return_value=asyncio.Future())
    mock_cursor.__aenter__.return_value.set_result(mock_cursor)
    mock_cursor.__aexit__ = MagicMock(return_value=asyncio.Future())
    mock_cursor.__aexit__.return_value.set_result(None)
    
    # Returning id
    mock_cursor.fetchone = MagicMock(return_value=asyncio.Future())
    mock_cursor.fetchone.return_value.set_result([123])
    
    mock_conn.execute = MagicMock(return_value=asyncio.Future())
    mock_conn.execute.return_value.set_result(mock_cursor)
    
    # loadgen.write_op doesn't return anything anymore, it updates known_max_id
    await loadgen.write_op(mock_conn, client_id=1, phase=0)
    assert loadgen.known_max_id == 123
    assert mock_conn.execute.called

@pytest.mark.asyncio
async def test_loadgen_read_op():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__aenter__ = MagicMock(return_value=asyncio.Future())
    mock_cursor.__aenter__.return_value.set_result(mock_cursor)
    mock_cursor.__aexit__ = MagicMock(return_value=asyncio.Future())
    mock_cursor.__aexit__.return_value.set_result(None)
    
    # Returning payload and checksum
    mock_cursor.fetchone = MagicMock(return_value=asyncio.Future())
    mock_cursor.fetchone.return_value.set_result(["payload", "abc"])
    
    mock_conn.execute = MagicMock(return_value=asyncio.Future())
    mock_conn.execute.return_value.set_result(mock_cursor)
    
    loadgen.known_max_id = 1
    # Mocking hashlib to avoid real work or just use real data
    await loadgen.read_op(mock_conn, phase=0)
    assert mock_conn.execute.called

@pytest.mark.asyncio
@patch("k8ostester.technologies.postgres_cnpg.loadgen.record")
async def test_loadgen_do_op(mock_record):
    mock_conn = MagicMock()
    
    # Success path
    with patch("k8ostester.technologies.postgres_cnpg.loadgen.write_op", return_value=asyncio.Future()) as mock_write:
        mock_write.return_value.set_result(None)
        await loadgen.do_op(mock_conn, client_id=1, phase_i=0, phase={"mix": {"read": 0, "write": 1}})
        # record is called INSIDE write_op, which we mocked!
        # So we need to either not mock write_op or expect record not to be called here.
        # Actually do_op calls write_op which calls record.
        
    # Let's not mock write_op to see it through
    mock_cursor = MagicMock()
    mock_cursor.__aenter__ = MagicMock(return_value=asyncio.Future())
    mock_cursor.__aenter__.return_value.set_result(mock_cursor)
    mock_cursor.fetchone = MagicMock(return_value=asyncio.Future())
    mock_cursor.fetchone.return_value.set_result([123])
    mock_conn.execute = MagicMock(return_value=asyncio.Future())
    mock_conn.execute.return_value.set_result(mock_cursor)
    
    await loadgen.do_op(mock_conn, client_id=1, phase_i=0, phase={"mix": {"read": 0, "write": 1}})
    mock_record.assert_called_with("write", 0, ANY, True, id=123, checksum=ANY)

@pytest.mark.asyncio
async def test_loadgen_safe_close():
    mock_conn = MagicMock()
    mock_conn.close = MagicMock(return_value=asyncio.Future())
    mock_conn.close.return_value.set_result(None)
    await loadgen.safe_close(mock_conn)
    mock_conn.close.assert_called_once()

@pytest.mark.asyncio
async def test_loadgen_connect():
    mock_psycopg.AsyncConnection.connect.return_value = asyncio.Future()
    mock_psycopg.AsyncConnection.connect.return_value.set_result(MagicMock())
    
    with patch("k8ostester.technologies.postgres_cnpg.loadgen.record") as mock_rec:
        conn = await loadgen.connect(0)
        assert conn is not None
        mock_rec.assert_called_with("connect", 0, ANY, True)

@pytest.mark.asyncio
async def test_loadgen_pooled_client():
    pool = MagicMock()
    pool.acquire.return_value = asyncio.Future()
    pool.acquire.return_value.set_result(MagicMock())
    
    phase = {"clients": 1, "rate": 100, "mix": {"write": 1}}
    
    with patch("k8ostester.technologies.postgres_cnpg.loadgen.do_op", return_value=asyncio.Future()) as mock_do, \
         patch("time.time", side_effect=[0, 10]): # Immediate deadline breach after 1 loop
        mock_do.return_value.set_result(True)
        await loadgen.pooled_client(pool, 1, 0, phase, deadline=5)
        assert pool.acquire.called
        assert mock_do.called

@pytest.mark.asyncio
async def test_loadgen_churn_client():
    with patch("k8ostester.technologies.postgres_cnpg.loadgen.connect", return_value=asyncio.Future()) as mock_conn, \
         patch("k8ostester.technologies.postgres_cnpg.loadgen.do_op", return_value=asyncio.Future()) as mock_do, \
         patch("time.time", side_effect=[0, 10]):
        
        conn = MagicMock()
        conn.close.return_value = asyncio.Future()
        conn.close.return_value.set_result(None)
        mock_conn.return_value.set_result(conn)
        mock_do.return_value.set_result(True)
        
        phase = {"clients": 1, "rate": 100, "mix": {"write": 1}}
        await loadgen.churn_client(1, 0, phase, deadline=5)
        assert mock_conn.called
        assert mock_do.called

@pytest.mark.asyncio
async def test_loadgen_main():
    with patch("k8ostester.technologies.postgres_cnpg.loadgen.asyncio.gather") as mock_gather, \
         patch("k8ostester.technologies.postgres_cnpg.loadgen.connect") as mock_conn:
        
        mock_gather.return_value = asyncio.Future()
        mock_gather.return_value.set_result([])
        
        # AsyncMock is better for this
        from unittest.mock import AsyncMock
        conn = AsyncMock()
        mock_conn.return_value = conn
        
        # Mock index/workers to cover shard logic
        os.environ["JOB_COMPLETION_INDEX"] = "0"
        with patch("k8ostester.technologies.postgres_cnpg.loadgen.PHASES", [{"duration_s": 1, "clients": 1, "rate": 1, "mode": "persistent"}]), \
             patch("k8ostester.technologies.postgres_cnpg.loadgen.WORKERS", 1):
            await loadgen.main()
            assert mock_gather.called
