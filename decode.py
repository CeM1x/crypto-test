"""Шаг 2. Из raw_logs строит ledger (все движения балансов) и order_fills (сделки кошелька)."""
import db
from config import NATIVE_TOKEN, WALLET

W = db.hb(WALLET)
NATIVE = db.hb(NATIVE_TOKEN)


def topic(sig):
    return db.keccak256(sig.encode())


T_TRANSFER = topic("Transfer(address,address,uint256)")
T_SINGLE = topic("TransferSingle(address,address,address,uint256,uint256)")
T_BATCH = topic("TransferBatch(address,address,address,uint256[],uint256[])")
T_NATIVE = topic("LogTransfer(address,address,address,uint256,uint256,uint256,uint256,uint256)")
T_FILL_V1 = topic("OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
T_FILL_V2 = topic("OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")


def word(data, i):
    return int.from_bytes(data[32 * i:32 * i + 32], "big")


def addr(t):
    return t[12:]


def uint_array(data, head_index):
    off = word(data, head_index)
    n = int.from_bytes(data[off:off + 32], "big")
    return [int.from_bytes(data[off + 32 * (k + 1):off + 32 * (k + 2)], "big") for k in range(n)]


def movements(address, t0, t1, t2, t3, data):
    """-> (standard, token, [(token_id, amount)], from, to) либо None, если лог не двигает баланс."""
    if t0 == T_TRANSFER and t1 and t2:
        if t3 is None:
            return "erc20", address, [(0, word(data, 0))], addr(t1), addr(t2)
        return "erc721", address, [(int.from_bytes(t3, "big"), 1)], addr(t1), addr(t2)
    if t0 == T_SINGLE:
        return "erc1155", address, [(word(data, 0), word(data, 1))], addr(t2), addr(t3)
    if t0 == T_BATCH:
        return "erc1155", address, list(zip(uint_array(data, 0), uint_array(data, 1))), addr(t2), addr(t3)
    if t0 == T_NATIVE and address == NATIVE:
        return "native", address, [(0, word(data, 0))], addr(t2), addr(t3)
    return None


def fill(address, t0, t1, t2, t3, data):
    """OrderFilled, где maker = кошелёк -> (version, order_hash, counterparty, side, token_id, shares, usd, fee)."""
    if t0 == T_FILL_V1 and addr(t2) == W:
        maker_asset, taker_asset, maker_amt, taker_amt, fee = (word(data, i) for i in range(5))
        if maker_asset == 0:
            return 1, t1, addr(t3), "BUY", taker_asset, taker_amt, maker_amt, fee
        return 1, t1, addr(t3), "SELL", maker_asset, maker_amt, taker_amt, fee
    if t0 == T_FILL_V2 and addr(t2) == W:
        side, token_id, maker_amt, taker_amt, fee = (word(data, i) for i in range(5))
        if side == 0:
            return 2, t1, addr(t3), "BUY", token_id, taker_amt, maker_amt, fee
        return 2, t1, addr(t3), "SELL", token_id, maker_amt, taker_amt, fee
    return None


def run():
    with db.connect() as conn, db.connect() as wconn, db.connect() as fconn:
        # два COPY одновременно на одном соединении невозможны — под order_fills своё соединение
        wconn.execute("TRUNCATE ledger, order_fills")
        wconn.commit()
        src = conn.cursor(name="raw")
        src.itersize = 50_000
        src.execute(
            "SELECT block_number, log_index, block_time, tx_hash, address, topic0, topic1, topic2, topic3, data "
            "FROM raw_logs ORDER BY block_number, log_index"
        )
        n_ledger = n_fills = 0
        with wconn.cursor() as c1, fconn.cursor() as c2, \
                c1.copy("COPY ledger (block_number, log_index, sub_index, block_time, tx_hash, standard, token, "
                        "token_id, delta, counterparty) FROM STDIN") as ledger, \
                c2.copy("COPY order_fills (block_number, log_index, block_time, tx_hash, exchange, version, "
                        "order_hash, counterparty, side, token_id, shares, usd, fee) FROM STDIN") as fills:
            for block, idx, ts, tx, address, t0, t1, t2, t3, data in src:
                m = movements(address, t0, t1, t2, t3, data)
                if m:
                    standard, token, items, frm, to = m
                    sub = 0
                    for token_id, amount in items:
                        # self-transfer даёт две строки (-x и +x), чтобы история оставалась полной
                        if frm == W:
                            ledger.write_row((block, idx, sub, ts, tx, standard, token, token_id, -amount, to))
                            sub += 1
                        if to == W:
                            ledger.write_row((block, idx, sub, ts, tx, standard, token, token_id, amount, frm))
                            sub += 1
                    n_ledger += sub
                f = fill(address, t0, t1, t2, t3, data)
                if f:
                    version, order_hash, cp, side, token_id, shares, usd, fee = f
                    fills.write_row((block, idx, ts, tx, address, version, order_hash, cp, side, token_id, shares, usd, fee))
                    n_fills += 1
        wconn.commit()
        fconn.commit()
        wconn.execute("ANALYZE ledger")
    print(f"ledger: {n_ledger} rows, order_fills: {n_fills} rows")


if __name__ == "__main__":
    run()
