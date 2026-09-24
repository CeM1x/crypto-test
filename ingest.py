"""Шаг 1. Выкачивает в raw_logs все логи Polygon, где кошелёк стоит в indexed-топике 1, 2 или 3

Фильтр по адресу контракта намеренно не задаётся: так в выборку попадает любой токен и любой
контракт, когда-либо упомянувший кошелёк, и баланс нельзя «потерять» из-за неизвестного контракта.
Скан идёт с блока 0 (токены могли прийти на адрес ещё до деплоя прокси) до зафиксированного блока head
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import db
import rpc
from config import CHUNK_BLOCKS, WALLET, WALLET_TOPIC, WORKERS

COLUMNS = ("block_number", "log_index", "block_time", "block_hash", "tx_hash", "tx_index",
           "address", "topic0", "topic1", "topic2", "topic3", "data")


def find_deploy_block(head):
    """Бинарный поиск блока, в котором у адреса появился код (нужен архивный узел)"""
    if rpc.call("eth_getCode", [WALLET, hex(head)]) == "0x":
        return None
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc.call("eth_getCode", [WALLET, hex(mid)]) != "0x":
            hi = mid
        else:
            lo = mid + 1
    return lo


def plan_chunks(conn, head):
    """Дорезает план сканирования до блока head. Возвращает, с какого блока начат докат"""
    last = conn.execute("SELECT max(to_block) FROM scan_chunks").fetchone()[0]
    if last is None:
        deploy = find_deploy_block(head)
        print(f"wallet deploy block: {deploy}")
        db.set_state(conn, "deploy_block", deploy)
        bounds = []
        # до деплоя активности почти нет — один большой кусок
        if deploy:
            bounds.append((0, deploy - 1))
        start = deploy or 0
    else:
        bounds, start = [], last + 1
    while start <= head:
        end = min(start + CHUNK_BLOCKS - 1, head)
        bounds.append((start, end))
        start = end + 1
    with conn.cursor() as cur:
        for pos in (1, 2, 3):
            cur.executemany(
                "INSERT INTO scan_chunks (topic_pos, from_block, to_block) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(pos, a, b) for a, b in bounds],
            )
    db.set_state(conn, "head_block", head)
    conn.commit()


def to_row(log):
    topics = [db.hb(t) for t in log["topics"]] + [None] * 4
    ts = log.get("blockTimestamp")
    return (
        int(log["blockNumber"], 16), int(log["logIndex"], 16),
        datetime.fromtimestamp(int(ts, 16), timezone.utc) if ts else None,
        db.hb(log["blockHash"]), db.hb(log["transactionHash"]), int(log["transactionIndex"], 16),
        db.hb(log["address"]), topics[0], topics[1], topics[2], topics[3], db.hb(log["data"]),
    )


def store(conn, logs):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS stage (LIKE raw_logs) ON COMMIT DELETE ROWS")
        with cur.copy(f"COPY stage ({', '.join(COLUMNS)}) FROM STDIN") as copy:
            for log in logs:
                copy.write_row(to_row(log))
        # один и тот же лог приходит несколько раз, если кошелёк стоит в нескольких топиках
        cur.execute("INSERT INTO raw_logs SELECT * FROM stage ON CONFLICT DO NOTHING")


def scan_chunk(pos, from_block, to_block):
    topics = [None] * pos + [WALLET_TOPIC]
    total = 0
    with db.connect() as conn:
        for _, _, logs in rpc.get_logs_range(from_block, to_block, topics):
            if logs:
                store(conn, logs)
                conn.commit()
                total += len(logs)
        conn.execute(
            "UPDATE scan_chunks SET done = true, logs_found = %s WHERE topic_pos = %s AND from_block = %s",
            (total, pos, from_block),
        )
        conn.commit()
    return total


def fill_missing_timestamps(conn):
    """Если узел не отдал blockTimestamp в логах — дотягиваем заголовки блоков"""
    blocks = [r[0] for r in conn.execute("SELECT DISTINCT block_number FROM raw_logs WHERE block_time IS NULL")]
    for i in range(0, len(blocks), 200):
        part = blocks[i:i + 200]
        res = rpc.batch_call([("eth_getBlockByNumber", [hex(b), False]) for b in part])
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE raw_logs SET block_time = to_timestamp(%s) WHERE block_number = %s",
                [(int(r["timestamp"], 16), b) for r, b in zip(res, part)],
            )
        conn.commit()


def run(head=None):
    with db.connect() as conn:
        db.init(conn)
        head = head or int(rpc.call("eth_blockNumber", []), 16)
        plan_chunks(conn, head)
        todo = conn.execute(
            "SELECT topic_pos, from_block, to_block FROM scan_chunks WHERE NOT done ORDER BY from_block DESC"
        ).fetchall()
    print(f"head block {head}; chunks to scan: {len(todo)}")
    started, done, found = time.time(), 0, 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(scan_chunk, *c) for c in todo]
        for f in as_completed(futures):
            found += f.result()
            done += 1
            if done % 10 == 0 or done == len(todo):
                print(f"  {done}/{len(todo)} chunks, {found} logs, {time.time() - started:.0f}s", flush=True)
    with db.connect() as conn:
        fill_missing_timestamps(conn)
        n = conn.execute("SELECT count(*) FROM raw_logs").fetchone()[0]
    print(f"raw_logs: {n} rows")
    return head


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else None)
