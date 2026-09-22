"""Шаг 3. Сверяет балансы, посчитанные из ledger, с balanceOf в блокчейне на блоке head.

Проверяются ВСЕ активы, которые когда-либо проходили через кошелёк, включая те, у которых
расчётный баланс равен нулю (on-chain там тоже обязан быть ноль).
"""
import sys

import db
import rpc
from config import CONTRACTS, NATIVE_TOKEN, WALLET

SEL_BALANCE_OF = "0x70a08231"
SEL_BALANCE_OF_BATCH = "0x4e1273f4"
BATCH = 400
W32 = "0" * 24 + WALLET[2:]


def enc_uint(x):
    return f"{x:064x}"


def erc1155_balances(token, ids, block):
    out = []
    for i in range(0, len(ids), BATCH):
        part = ids[i:i + BATCH]
        n = len(part)
        data = (SEL_BALANCE_OF_BATCH + enc_uint(0x40) + enc_uint(0x40 + 32 * (n + 1))
                + enc_uint(n) + W32 * n + enc_uint(n) + "".join(enc_uint(x) for x in part))
        res = bytes.fromhex(rpc.call("eth_call", [{"to": token, "data": data}, hex(block)])[2:])
        assert int.from_bytes(res[32:64], "big") == n
        out += [int.from_bytes(res[64 + 32 * k:96 + 32 * k], "big") for k in range(n)]
    return out


def erc20_balance(token, block):
    try:
        res = rpc.call("eth_call", [{"to": token, "data": SEL_BALANCE_OF + W32}, hex(block)])
        return int(res, 16) if res and res != "0x" else None
    except rpc.RpcError:
        return None  # контракт ревертит / самоуничтожен (типично для спам-токенов)


def is_fake_token(token, block):
    """Фишинговые airdrop-токены: balanceOf ревертит либо отдаёт одну и ту же ненулевую
    константу для любого адреса. Честного on-chain баланса у них нет — сверять не с чем."""
    probes = []
    for seed in (b"probe-1", b"probe-2"):
        a = db.keccak256(seed)[12:].hex()
        try:
            res = rpc.call("eth_call", [{"to": token, "data": SEL_BALANCE_OF + "0" * 24 + a}, hex(block)])
            probes.append(int(res, 16) if res and res != "0x" else None)
        except rpc.RpcError:
            probes.append(None)
    return probes[0] is None or (probes[0] == probes[1] and probes[0] != 0)


def run(block=None):
    with db.connect() as conn:
        block = block or int(db.get_state(conn, "head_block"))
        pending = conn.execute("SELECT count(*) FROM scan_chunks WHERE NOT done OR to_block > %s", (block,)).fetchone()[0]
        covered = conn.execute("SELECT max(to_block) FROM scan_chunks WHERE done").fetchone()[0]
        if pending or covered != block:
            sys.exit(f"scan is not complete up to block {block} (covered {covered}, pending chunks {pending})")

        computed = conn.execute(
            "SELECT standard, token, token_id, sum(delta) FROM ledger GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()
        rows = []
        by_1155 = {}
        for standard, token, token_id, bal in computed:
            token_hex = "0x" + token.hex()
            if standard == "erc1155":
                by_1155.setdefault(token_hex, []).append((int(token_id), int(bal)))
            elif standard == "native":
                onchain = int(rpc.call("eth_getBalance", [WALLET, hex(block)]), 16)
                rows.append((standard, token, 0, int(bal), onchain))
            else:  # erc20 / erc721 — у обоих balanceOf(address)
                rows.append((standard, token, int(token_id), int(bal), None))
        if not any(r[0] == "native" for r in rows):  # движений POL не было — on-chain обязан быть 0
            rows.append(("native", db.hb(NATIVE_TOKEN), 0, 0, int(rpc.call("eth_getBalance", [WALLET, hex(block)]), 16)))
        # erc721 хранится по token_id, а balanceOf отдаёт количество — сворачиваем до одного значения на контракт
        folded = {}
        for standard, token, token_id, bal, onchain in rows:
            key = (standard, token, 0)
            folded[key] = (folded.get(key, (0, None))[0] + bal, onchain)
        rows = []
        for (standard, token, _), (bal, onchain) in folded.items():
            if standard != "native":
                onchain = erc20_balance("0x" + token.hex(), block)
            rows.append((standard, token, 0, bal, onchain))
        for token_hex, items in by_1155.items():
            chain = erc1155_balances(token_hex, [i for i, _ in items], block)
            rows += [("erc1155", db.hb(token_hex), i, b, c) for (i, b), c in zip(items, chain)]

        fake = {t for s, t, i, b, c in rows if s in ("erc20", "erc721") and b != c and is_fake_token("0x" + t.hex(), block)}

        conn.execute("DELETE FROM balance_check WHERE checked_block = %s", (block,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO balance_check VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(block, s, t, i, b, c, b == c, "fake token: balanceOf is constant/reverts" if t in fake else None)
                 for s, t, i, b, c in rows],
            )
        conn.commit()

    print(f"\nСверка на блоке {block}: активов проверено {len(rows)}")
    bad = [r for r in rows if r[3] != r[4] and r[1] not in fake]
    for s, t, i, b, c in rows:
        label = CONTRACTS.get("0x" + t.hex())
        if s != "erc1155":
            print(f"  {'OK ' if b == c else 'FAKE' if t in fake else 'BAD'} {s:7} {label or '0x' + t.hex():28} computed={b} onchain={c}")
    for token_hex in by_1155:
        sub = [r for r in rows if r[0] == "erc1155" and r[1] == db.hb(token_hex)]
        ok = sum(1 for r in sub if r[3] == r[4])
        nonzero = sum(1 for r in sub if r[4])
        print(f"  {'OK ' if ok == len(sub) else 'BAD'} erc1155 {CONTRACTS.get(token_hex, token_hex):28} "
              f"token ids: {len(sub)}, совпало: {ok}, с ненулевым балансом: {nonzero}")
    for s, t, i, b, c in bad[:50]:
        print(f"  MISMATCH {s} 0x{t.hex()} id={i}: computed={b} onchain={c} diff={None if c is None else b - c}")
    if fake:
        print(f"  FAKE = фишинговые airdrop-токены ({len(fake)} шт.): их balanceOf не зависит от адреса или ревертит, в итог не входят")
    print("ИТОГ:", "все балансы сошлись" if not bad else f"расхождений: {len(bad)}")
    return not bad


if __name__ == "__main__":
    sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else None) else 1)
