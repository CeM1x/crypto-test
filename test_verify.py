"""Сценарии отказов: сбой RPC, реверт, потерянный при загрузке актив, потерянные логи.
Нужна заполненная БД (после run.py). Запуск: .venv/bin/python -m unittest test_verify -v"""
import unittest
from unittest import mock

import db
import rpc
import verify

PUSD = db.hb("0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb")
PUSD_KEY = ("erc20", PUSD, 0)


def failing_call(match):
    """rpc.call, который падает только на eth_call к заданному контракту"""
    real = rpc.call

    def fake(method, params, *a, **kw):
        if method == "eth_call" and params[0]["to"] == match:
            raise kw.pop("_exc") if "_exc" in kw else fake.exc
        return real(method, params, *a, **kw)
    return fake


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
