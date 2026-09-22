"""Полный цикл: докачать логи до текущего блока -> пересобрать ledger -> сверить с блокчейном."""
import sys

import decode
import ingest
import verify

if __name__ == "__main__":
    head = ingest.run()
    decode.run()
    sys.exit(0 if verify.run(head) else 1)
