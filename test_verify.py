"""Сценарии отказов: сбой RPC, реверт, усечённые ответы, фейковые ERC-1155, потерянные логи

FakeChainTests — без БД и без сети (rpc.call подменён), запуск: .venv/bin/python -m unittest test_verify.FakeChainTests -v
VerifyFailures — нужна заполненная БД (после run.py):   .venv/bin/python -m unittest test_verify -v"""
import unittest
from unittest import mock

import db
import rpc
import verify

PUSD = db.hb("0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb")
PUSD_KEY = ("erc20", PUSD, 0)


def failing_call(match):
    """rpc.call, который падает только на eth_call к заданному контракту."""
    real = rpc.call

    def fake(method, params, *a, **kw):
        if method == "eth_call" and params[0]["to"] == match:
            raise kw.pop("_exc") if "_exc" in kw else fake.exc
        return real(method, params, *a, **kw)
    return fake


CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
SCAM_1155 = "0x00000000000000000000000000000000000f4ce1"
SCAM_20 = "0x00000000000000000000000000000000000f4ce2"
BLOCK = 1
REVERT = rpc.RpcError({"code": 3, "message": "execution reverted"})


def enc(x):
    return f"{x:064x}"


def abi_array(vals):
    return "0x" + enc(0x20) + enc(len(vals)) + "".join(enc(v) for v in vals)


class Chain:
    """Двойник rpc.call: ответ eth_call задаётся обработчиками по селектору.
    batch(holder32, ids) / single(holder32, id) / erc20(holder32) -> hex-строка результата либо исключение"""

    def __init__(self, batch=None, single=None, erc20=None):
        self.batch, self.single, self.erc20 = batch, single, erc20
        self.calls = []

    def __call__(self, method, params, *a, **kw):
        assert method == "eth_call", method
        data = params[0]["data"]
        sel, body = data[:10], data[10:]
        self.calls.append(sel)
        if sel == verify.SEL_BALANCE_OF_BATCH:
            words = [int(body[64 * k:64 * k + 64], 16) for k in range(len(body) // 64)]
            n, holder = words[2], body[192:256]
            ids = words[4 + n:4 + 2 * n]
            handler, args = self.batch, (holder, ids)
        elif sel == verify.SEL_BALANCE_OF_1155:
            handler, args = self.single, (body[:64], int(body[64:128], 16))
        else:
            handler, args = self.erc20, (body[:64],)
        if handler is None:
            raise REVERT
        res = handler(*args)
        if isinstance(res, Exception):
            raise res
        return res


def statuses(rows):
    return sorted({r[5] for r in rows})


class FakeChainTests(unittest.TestCase):
    """Без БД и сети"""

    def check_1155(self, chain, token, computed, **patch):
        with mock.patch.object(rpc, "call", chain), mock.patch.multiple(verify, BATCH=verify.BATCH, **patch):
            return verify.fetch_onchain({("erc1155", db.hb(token), i): b for i, b in computed.items()}, BLOCK)

    def test_truncated_batch_is_not_read_as_zero(self):
        # length-слово говорит «3 баланса», но третьего в данных нет: раньше он читался как 0 и совпадал с расчётом 0
        truncated = abi_array([5, 7, 0])[:-64]
        rows = self.check_1155(Chain(batch=lambda h, ids: truncated), CTF, {1: 5, 2: 7, 3: 0})
        self.assertEqual(statuses(rows), [verify.BAD])
        self.assertTrue(all(r[4] is None for r in rows))
        self.assertIn("malformed", rows[0][6])
        self.assertFalse(verify.report(BLOCK, rows, [("x", True, 0)]))

    def test_batch_with_wrong_length_word(self):
        rows = self.check_1155(Chain(batch=lambda h, ids: abi_array([5, 7])), CTF, {1: 5, 2: 7, 3: 0})
        self.assertEqual(statuses(rows), [verify.BAD])

    def test_batch_with_extra_data_is_malformed(self):
        rows = self.check_1155(Chain(batch=lambda h, ids: abi_array([5, 7, 0]) + enc(1)), CTF, {1: 5, 2: 7, 3: 0})
        self.assertEqual(statuses(rows), [verify.BAD])

    def test_truncated_batch_is_not_repaired_via_single_balance_of(self):
        # даже если balanceOf(address,id) работает, битый ответ batch — ошибка, а не повод сходить другим путём
        truncated = abi_array([5, 7, 0])[:-64]
        chain = Chain(batch=lambda h, ids: truncated, single=lambda h, i: "0x" + enc({1: 5, 2: 7, 3: 0}[i]))
        rows = self.check_1155(chain, CTF, {1: 5, 2: 7, 3: 0})
        self.assertEqual(statuses(rows), [verify.BAD])
        self.assertNotIn(verify.SEL_BALANCE_OF_1155, chain.calls)

    def test_reverting_batch_falls_back_to_single_balance_of(self):
        onchain = {1: 5, 2: 7, 3: 9}
        chain = Chain(batch=lambda h, ids: REVERT, single=lambda h, i: "0x" + enc(onchain[i]))
        rows = self.check_1155(chain, CTF, {1: 5, 2: 7, 3: 0})
        self.assertEqual([r[5] for r in rows], [verify.OK, verify.OK, verify.BAD])
        self.assertEqual(rows[2][4], 9)

    def test_no_single_fallback_for_huge_id_sets(self):
        chain = Chain(batch=lambda h, ids: REVERT, single=lambda h, i: "0x" + enc(0))
        rows = self.check_1155(chain, CTF, {i: 0 for i in range(verify.SINGLE_FALLBACK_MAX + 1)})
        self.assertEqual(statuses(rows), [verify.BAD])
        self.assertNotIn(verify.SEL_BALANCE_OF_1155, chain.calls)

    def test_unknown_1155_reverting_for_everyone_is_fake(self):
        rows = self.check_1155(Chain(), SCAM_1155, {1: 3, 2: 1})
        self.assertEqual(statuses(rows), [verify.FAKE])
        self.assertTrue(verify.report(BLOCK, rows, [("x", True, 0)]))  # фейк не блокирует «сошлось»

    def test_unknown_1155_without_batch_but_with_balance_of_is_verified(self):
        chain = Chain(single=lambda h, i: "0x" + enc({1: 3, 2: 1}[i] if h == verify.W32 else 0))
        rows = self.check_1155(chain, SCAM_1155, {1: 3, 2: 1})
        self.assertEqual(statuses(rows), [verify.OK])

    def test_unknown_1155_constant_for_everyone_is_fake(self):
        rows = self.check_1155(Chain(batch=lambda h, ids: abi_array([1] * len(ids))), SCAM_1155, {1: 3, 2: 50})
        self.assertEqual(statuses(rows), [verify.FAKE])

    def test_unknown_1155_reverting_for_wallet_only_is_bad(self):
        chain = Chain(batch=lambda h, ids: REVERT if h == verify.W32 else abi_array([0] * len(ids)))
        rows = self.check_1155(chain, SCAM_1155, {1: 3})
        self.assertEqual(statuses(rows), [verify.BAD])

    def test_unknown_1155_honest_mismatch_is_bad(self):
        chain = Chain(batch=lambda h, ids: abi_array([3, 4] if h == verify.W32 else [0, 0]))
        rows = self.check_1155(chain, SCAM_1155, {1: 3, 2: 1})
        self.assertEqual([r[5] for r in rows], [verify.OK, verify.BAD])

    def test_known_1155_revert_is_bad_not_fake(self):
        rows = self.check_1155(Chain(), CTF, {1: 0})
        self.assertEqual(statuses(rows), [verify.BAD])

    def test_rpc_outage_during_fake_probe_is_unverified(self):
        chain = Chain(batch=lambda h, ids: REVERT if h == verify.W32 else rpc.RpcUnavailable("timeout"))
        rows = self.check_1155(chain, SCAM_1155, {1: 3})
        self.assertEqual(statuses(rows), [verify.UNVERIFIED])

    def test_erc20_short_return_data_is_not_a_balance(self):
        chain = Chain(erc20=lambda h: "0x01")
        with mock.patch.object(rpc, "call", chain):
            rows = verify.fetch_onchain({("erc20", PUSD, 0): 1}, BLOCK)
        self.assertEqual(rows[0][5], verify.BAD)
        self.assertIn("malformed", rows[0][6])

    def test_garbage_rpc_result_is_unverified(self):
        chain = Chain(erc20=lambda h: "not-hex")
        with mock.patch.object(rpc, "call", chain):
            rows = verify.fetch_onchain({("erc20", PUSD, 0): 1}, BLOCK)
        self.assertEqual(rows[0][5], verify.UNVERIFIED)


class VerifyFailures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()
        cls.block = int(db.get_state(cls.conn, "head_block"))
        cls.pusd_balance = verify.balance_of("0x" + PUSD.hex(), verify.W32, cls.block)
        assert cls.pusd_balance > 0, "тест ожидает ненулевой pUSD на кошельке"

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def test_rpc_outage_marks_unverified_not_fake(self):
        fake = failing_call("0x" + PUSD.hex())
        fake.exc = rpc.RpcUnavailable("connection refused")
        with mock.patch.object(rpc, "call", fake):
            rows = verify.fetch_onchain({PUSD_KEY: self.pusd_balance}, self.block)
        self.assertEqual(rows[0][5], verify.UNVERIFIED)
        self.assertFalse(verify.report(self.block, rows, [("x", True, 0)]))

    def test_non_revert_rpc_error_marks_unverified(self):
        fake = failing_call("0x" + PUSD.hex())
        fake.exc = rpc.RpcError({"code": -32005, "message": "rate limit exceeded"})
        with mock.patch.object(rpc, "call", fake):
            rows = verify.fetch_onchain({PUSD_KEY: self.pusd_balance}, self.block)
        self.assertEqual(rows[0][5], verify.UNVERIFIED)

    def test_known_token_revert_is_bad_not_fake(self):
        fake = failing_call("0x" + PUSD.hex())
        fake.exc = rpc.RpcError({"code": 3, "message": "execution reverted"})
        with mock.patch.object(rpc, "call", fake):
            rows = verify.fetch_onchain({PUSD_KEY: self.pusd_balance}, self.block)
        self.assertEqual(rows[0][5], verify.BAD)

    def test_unknown_reverting_token_is_fake(self):
        scam = "0x050c2e22114b771050cd77b00efb3b8576ce2e06"  # реальный фишинговый токен из истории кошелька
        rows = verify.fetch_onchain({("erc20", db.hb(scam), 0): 1}, self.block)
        self.assertEqual(rows[0][5], verify.FAKE)

    def test_asset_lost_during_ingest_is_still_checked(self):
        self.conn.execute("DELETE FROM ledger WHERE token = %s", (PUSD,))  # имитация: pUSD не загрузился вовсе
        computed = verify.load_computed(self.conn)
        self.conn.rollback()
        self.assertEqual(computed[PUSD_KEY], 0)
        rows = verify.fetch_onchain({PUSD_KEY: computed[PUSD_KEY]}, self.block)
        self.assertEqual(rows[0][5], verify.BAD)
        self.assertEqual(rows[0][4], self.pusd_balance)

    def test_audit_catches_lost_ctf_logs(self):
        tx = self.conn.execute("SELECT tx_hash FROM order_fills LIMIT 1").fetchone()[0]
        self.conn.execute("DELETE FROM ledger WHERE tx_hash = %s AND standard = 'erc1155'", (tx,))
        failed = [name for name, ok, _ in verify.audit(self.conn) if not ok]
        self.conn.rollback()
        self.assertIn("order_fills without matching ERC-1155 move in same tx", failed)

    def test_audit_catches_lost_raw_logs(self):
        self.conn.execute("DELETE FROM raw_logs WHERE block_number = (SELECT max(block_number) FROM raw_logs)")
        failed = [name for name, ok, _ in verify.audit(self.conn) if not ok]
        self.conn.rollback()
        self.assertIn("scan_chunks whose logs_found differs from rows stored in raw_logs", failed)

    def test_audit_passes_on_intact_db(self):
        self.assertTrue(all(ok for _, ok, _ in verify.audit(self.conn)))


if __name__ == "__main__":
    unittest.main()
