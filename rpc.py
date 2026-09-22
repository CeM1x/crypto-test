"""Минимальный JSON-RPC клиент для Polygon: ретраи, запасные узлы, адаптивный eth_getLogs."""
import re
import threading
import time

import requests

from config import FALLBACK_MAX_SPAN, RPC_URLS

_local = threading.local()
_RANGE_HINT = re.compile(r"\[(0x[0-9a-fA-F]+|earliest),\s*(0x[0-9a-fA-F]+)\]")
_TOO_MANY = ("more than", "too many", "exceed", "limit", "too large", "response size")


class RpcError(Exception):
    def __init__(self, error):
        super().__init__(str(error))
        self.error = error
        self.text = (str(error.get("message", "")) + " " + str(error.get("data", ""))) if isinstance(error, dict) else str(error)


def _session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def _post(url, payload):
    r = _session().post(url, json=payload, timeout=120)
    if r.status_code == 429 or r.status_code >= 500:
        raise requests.HTTPError(f"HTTP {r.status_code}")
    return r.json()


def call(method, params, url=None, retries=6):
    """Один RPC-вызов к основному (или указанному) узлу с ретраями на сетевые ошибки."""
    url = url or RPC_URLS[0]
    for attempt in range(retries):
        try:
            resp = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except (requests.RequestException, ValueError):
            time.sleep(min(2 ** attempt, 30))
            continue
        if "error" in resp:
            raise RpcError(resp["error"])
        return resp["result"]
    raise RpcError({"message": f"{method}: no response from {url} after {retries} attempts"})


def batch_call(calls, url=None):
    """calls: [(method, params)] -> список результатов в том же порядке."""
    url = url or RPC_URLS[0]
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    for attempt in range(6):
        try:
            resp = _post(url, payload)
            if isinstance(resp, list) and len(resp) == len(calls) and all("result" in x for x in resp):
                by_id = {x["id"]: x["result"] for x in resp}
                return [by_id[i] for i in range(len(calls))]
        except (requests.RequestException, ValueError):
            pass
        time.sleep(min(2 ** attempt, 30))
    # узел не умеет/не хочет батчи — по одному
    return [call(m, p, url) for m, p in calls]


def get_logs_range(from_block, to_block, topics, address=None):
    """Генератор (from, to, logs): покрывает [from_block, to_block] без пропусков,
    сужая диапазон, когда узел отказывается отдавать слишком большой ответ."""
    cur = from_block
    span = to_block - from_block + 1
    while cur <= to_block:
        end = min(cur + span - 1, to_block)
        flt = {"fromBlock": hex(cur), "toBlock": hex(end), "topics": topics}
        if address:
            flt["address"] = address
        try:
            logs = _get_logs_once(flt, end - cur + 1)
        except RpcError as e:
            if cur == end:
                raise
            hint = _RANGE_HINT.search(e.text)
            if hint and int(hint.group(2), 16) >= cur and int(hint.group(2), 16) < end:
                span = int(hint.group(2), 16) - cur + 1
            else:
                span = max(1, (end - cur + 1) // 2)
            continue
        if any(l.get("removed") for l in logs):
            raise RpcError({"message": "removed log returned (reorg) — retry later"})
        yield cur, end, logs
        cur = end + 1
        span = min(span * 2, to_block - cur + 1) if cur <= to_block else span


def _get_logs_once(flt, span):
    urls = RPC_URLS if span <= FALLBACK_MAX_SPAN else RPC_URLS[:1]
    last = None
    for url in urls:
        try:
            return call("eth_getLogs", [flt], url=url, retries=4)
        except RpcError as e:
            last = e
            if any(m in e.text.lower() for m in _TOO_MANY):
                raise  # нужно сужать диапазон, а не менять узел
    raise last
