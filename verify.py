"""Шаг 3. Сверяет балансы, посчитанные из ledger, с balanceOf в блокчейне на блоке head.

Принципы:
  * проверяются ВСЕ активы, когда-либо проходившие через кошелёк, плюс CORE_ASSETS - всегда,
    даже если в загруженных логах их нет (тогда расчёт = 0, и on-chain обязан быть 0);
  * сбой RPC - это не свойство контракта: актив получает статус UNVERIFIED, а не FAKE/OK;
  * FAKE присваивается только неизвестным контрактам, чей balanceOf ревертит или отдаёт
    одну константу любому адресу; контракты из CONTRACTS фейком быть не могут;
  * отдельно выполняется аудит полноты по независимым потокам событий (биржа / CTF / ERC-20);
  * «все балансы сошлись» печатается только если нет ни BAD, ни UNVERIFIED и аудит пройден.

Коды возврата: 0 - сошлось, 1 - расхождения/непроверенные/аудит, 2 - сверка не выполнена
"""
import sys

import db
import rpc
from config import CONTRACTS, CORE_ASSETS, NATIVE_TOKEN, WALLET, WALLET_TOPIC

SEL_BALANCE_OF = "0x70a08231"
SEL_BALANCE_OF_BATCH = "0x4e1273f4"
BATCH = 400
W32 = "0" * 24 + WALLET[2:]
USD_TOKENS = [db.hb(a) for a, s in CORE_ASSETS.items() if s == "erc20"]

OK, BAD, FAKE, UNVERIFIED = "OK", "BAD", "FAKE", "UNVERIFIED"


class Reverted(Exception):
    """balanceOf ревертит либо по адресу нет кода - свойство контракта, не сбой"""


def enc_uint(x):
    return f"{x:064x}"


def eth_call(to, data, block):
    res = rpc.call("eth_call", [{"to": to, "data": data}, hex(block)])
    if res in (None, "", "0x"):
        raise Reverted("empty return data")
    return res


def balance_of(token, holder32, block):
    try:
        return int(eth_call(token, SEL_BALANCE_OF + holder32, block), 16)
    except rpc.RpcError as e:
        if e.is_revert():
            raise Reverted(e.text.strip()) from e
        raise  # любая другая JSON-RPC ошибка - сбой узла, пробрасываем


def erc1155_balances(token, ids, block):
    out = []
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        n = len(part)
        data = (SEL_BALANCE_OF_BATCH + enc_uint(0x40) + enc_uint(0x40 + 32 * (n + 1))
                + enc_uint(n) + W32 * n + enc_uint(n) + "".join(enc_uint(x) for x in part))
        res = bytes.fromhex(eth_call(token, data, block)[2:])
        if int.from_bytes(res[32:64], "big") != n:
            raise rpc.RpcUnavailable(f"balanceOfBatch({token}) returned malformed data")
        out += [int.from_bytes(res[64 + 32 * k:96 + 32 * k], "big") for k in range(n)]
    return out


def is_fake_token(token, block, wallet_balance):
    """Фишинговый airdrop-токен: balanceOf ревертит для всех либо отдаёт одну и ту же
    ненулевую константу любому адресу (и кошельку тоже). Известные контракты - никогда"""
    if token in CONTRACTS:
        return False
    probes = []
    for seed in (b"probe-1", b"probe-2"):
        try:
            probes.append(balance_of(token, "0" * 24 + db.keccak256(seed)[12:].hex(), block))
        except Reverted:
            probes.append(None)
    if probes == [None, None]:
        return True
    return probes[0] == probes[1] == wallet_balance and wallet_balance not in (None, 0)


def load_computed(conn):
    """{(standard, token_bytes, token_id): balance} из ledger + CORE_ASSETS с нулём, если их там нет"""
    computed = {}
    for standard, token, token_id, bal in conn.execute(
            "SELECT standard, token, token_id, sum(delta) FROM ledger GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"):
        if standard in ("erc20", "erc721"):
            token_id = 0  # erc721 хранится по id, а balanceOf отдаёт количество - сворачиваем
        key = (standard, bytes(token), int(token_id))
        computed[key] = computed.get(key, 0) + int(bal)
    for addr, standard in CORE_ASSETS.items():
        computed.setdefault((standard, db.hb(addr), 0), 0)
    return computed


def fetch_onchain(computed, block):
    """-> список (standard, token, token_id, computed, onchain, status, note)"""
    rows = []
    by_1155 = {}
    for (standard, token, token_id), bal in computed.items():
        token_hex = "0x" + token.hex()
        if standard == "erc1155":
            by_1155.setdefault(token_hex, []).append((token_id, bal))
            continue
        try:
            if standard == "native":
                onchain = int(rpc.call("eth_getBalance", [WALLET, hex(block)]), 16)
            else:
                onchain = balance_of(token_hex, W32, block)
            status, note = (OK, None) if onchain == bal else (BAD, None)
            if status == BAD and is_fake_token(token_hex, block, onchain):
                status, note = FAKE, "balanceOf is constant for any address"
        except Reverted as e:
            onchain = None
            if is_fake_token(token_hex, block, None):
                status, note = FAKE, f"balanceOf reverts for any address: {e}"
            else:
                status, note = BAD, f"balanceOf reverted for wallet only: {e}"
        except (rpc.RpcError, rpc.RpcUnavailable) as e:
            onchain, status, note = None, UNVERIFIED, f"rpc failure: {str(e)[:200]}"
        rows.append((standard, token, 0, bal, onchain, status, note))
    for token_hex, items in by_1155.items():
        token = db.hb(token_hex)
        try:
            chain = erc1155_balances(token_hex, [i for i, _ in items], block)
            rows += [("erc1155", token, i, b, c, OK if b == c else BAD, None) for (i, b), c in zip(items, chain)]
        except (Reverted, rpc.RpcError, rpc.RpcUnavailable) as e:
            note = f"rpc failure: {str(e)[:200]}"
            rows += [("erc1155", token, i, b, None, UNVERIFIED, note) for i, b in items]
    return rows


def audit(conn):
    """Проверки полноты загрузки/декодирования по независимым источникам. -> [(name, ok, detail)]"""
    checks = []

    def check(name, sql, params=(), expect_zero=True):
        n = conn.execute(sql, params).fetchone()[0]
        checks.append((name, n == 0 if expect_zero else n > 0, n))

    # Каждая сделка (лог биржи) обязана сопровождаться движением того же token_id в CTF (лог CTF)
    check("order_fills without matching ERC-1155 move in same tx",
          "SELECT count(*) FROM order_fills f WHERE NOT EXISTS (SELECT 1 FROM ledger l "
          "WHERE l.tx_hash = f.tx_hash AND l.standard = 'erc1155' AND l.token_id = f.token_id)")
    # ...и движением USD (лог ERC-20)
    check("order_fills without matching USD move in same tx",
          "SELECT count(*) FROM order_fills f WHERE NOT EXISTS (SELECT 1 FROM ledger l "
          "WHERE l.tx_hash = f.tx_hash AND l.standard = 'erc20' AND l.token = ANY(%s))", (USD_TOKENS,))
    # Каждый Transfer-подобный сырой лог с участием кошелька обязан дать строку ledger (целостность decode)
    w = db.hb(WALLET_TOPIC)  # в топиках адрес лежит 32-байтным словом
    t_erc20, t_single, t_batch, t_native = (db.keccak256(s.encode()) for s in (
        "Transfer(address,address,uint256)",
        "TransferSingle(address,address,address,uint256,uint256)",
        "TransferBatch(address,address,address,uint256[],uint256[])",
        "LogTransfer(address,address,address,uint256,uint256,uint256,uint256,uint256)"))
    check("raw transfer logs not decoded into ledger",
          "SELECT count(*) FROM raw_logs r WHERE ((topic0 = %s AND topic1 IS NOT NULL AND topic2 IS NOT NULL AND (topic1 = %s OR topic2 = %s))"
          " OR (topic0 IN (%s, %s) AND (topic2 = %s OR topic3 = %s))"
          " OR (topic0 = %s AND address = %s AND (topic2 = %s OR topic3 = %s)))"
          " AND NOT EXISTS (SELECT 1 FROM ledger l WHERE l.block_number = r.block_number AND l.log_index = r.log_index)",
          (t_erc20, w, w, t_single, t_batch, w, w, t_native, db.hb(NATIVE_TOKEN), w, w))
    # Число логов, которое узел вернул для чанка, обязано совпадать с тем, что лежит в raw_logs
    check("scan_chunks whose logs_found differs from rows stored in raw_logs",
          "SELECT count(*) FROM scan_chunks c WHERE c.done AND c.logs_found <> (SELECT count(*) FROM raw_logs r "
          "WHERE r.block_number BETWEEN c.from_block AND c.to_block "
          "AND CASE c.topic_pos WHEN 1 THEN r.topic1 WHEN 2 THEN r.topic2 ELSE r.topic3 END = %s)", (w,))
    check("ledger has rows at all", "SELECT count(*) FROM ledger", expect_zero=False)
    return checks


def run(block=None):
    with db.connect() as conn:
        block = block or int(db.get_state(conn, "head_block"))
        pending = conn.execute("SELECT count(*) FROM scan_chunks WHERE NOT done OR to_block > %s", (block,)).fetchone()[0]
        covered = conn.execute("SELECT max(to_block) FROM scan_chunks WHERE done").fetchone()[0]
        if pending or covered != block:
            raise RuntimeError(f"scan is not complete up to block {block} (covered {covered}, pending chunks {pending})")

        rows = fetch_onchain(load_computed(conn), block)
        checks = audit(conn)

        conn.execute("DELETE FROM balance_check WHERE checked_block = %s", (block,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO balance_check VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(block, s, t, i, b, c, st == OK, st if note is None else f"{st}: {note}")
                 for s, t, i, b, c, st, note in rows],
            )
        ok = report(block, rows, checks)
        db.set_state(conn, "last_verify", f"{block}:{'ok' if ok else 'failed'}")
        conn.commit()
    return ok


def report(block, rows, checks):
    print(f"\nСверка на блоке {block}: активов проверено {len(rows)}")
    by_status = {s: sum(1 for r in rows if r[5] == s) for s in (OK, BAD, FAKE, UNVERIFIED)}
    for s, t, i, b, c, st, note in rows:
        if s != "erc1155":
            label = CONTRACTS.get("0x" + t.hex()) or "0x" + t.hex()
            print(f"  {st:10} {s:7} {label:28} computed={b} onchain={c}" + (f"  [{note}]" if note else ""))
    for token in {r[1] for r in rows if r[0] == "erc1155"}:
        sub = [r for r in rows if r[0] == "erc1155" and r[1] == token]
        st = {r[5] for r in sub}
        st = OK if st == {OK} else UNVERIFIED if UNVERIFIED in st else BAD
        print(f"  {st:10} erc1155 {CONTRACTS.get('0x' + token.hex(), '0x' + token.hex()):28} token ids: {len(sub)}, "
              f"совпало: {sum(1 for r in sub if r[5] == OK)}, с ненулевым балансом: {sum(1 for r in sub if r[4])}")
    for s, t, i, b, c, st, note in [r for r in rows if r[5] == BAD][:50]:
        print(f"  MISMATCH {s} 0x{t.hex()} id={i}: computed={b} onchain={c} diff={None if c is None else b - c}")
    if by_status[FAKE]:
        print(f"  FAKE = фишинговые airdrop-токены ({by_status[FAKE]} шт.): honest on-chain баланса нет, в итог не входят")
    print("Аудит полноты:")
    for name, good, n in checks:
        print(f"  {'OK ' if good else 'FAIL'} {name}: {n}")
    audit_failed = sum(1 for _, good, _ in checks if not good)
    ok = not by_status[BAD] and not by_status[UNVERIFIED] and not audit_failed
    print("ИТОГ:", "все балансы сошлись" if ok else
          f"СВЕРКА НЕ ПРОЙДЕНА - BAD: {by_status[BAD]}, UNVERIFIED: {by_status[UNVERIFIED]}, аудит: {audit_failed} fail")
    return ok


if __name__ == "__main__":
    try:
        sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else None) else 1)
    except Exception as e:  # noqa: BLE001 - любая авария = сверка не выполнена, это не «сошлось»
        print(f"ИТОГ: СВЕРКА НЕ ВЫПОЛНЕНА: {type(e).__name__}: {e}")
        sys.exit(2)
