"""Полный цикл: докачать логи до текущего блока -> пересобрать ledger -> сверить с блокчейном

Коды возврата: 0 — балансы сошлись, 1 — расхождения / непроверенные активы, 2 — авария"""
import sys

import decode
import ingest
import verify

if __name__ == "__main__":
    try:
        head = ingest.run()
        decode.run()
        ok = verify.run(head)
    except Exception as e:  # noqa: BLE001
        print(f"ИТОГ: СВЕРКА НЕ ВЫПОЛНЕНА: {type(e).__name__}: {e}")
        sys.exit(2)
    sys.exit(0 if ok else 1)
