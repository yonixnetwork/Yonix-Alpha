"""A fake EVM JSON-RPC node for tests (httpx.MockTransport): eth_call
answers keyed by (contract, selector), eth_getLogs filtered like a real node
(or not, to test emitter validation), blocks, code and chain id. Proves
decoding / quoting logic only, never real-chain behaviour."""

import json

import httpx
from eth_abi import encode

from yonixalpha_core.chains.evm.abi import selector
from yonixalpha_core.chains.evm.rpc import EvmRpc

TX = "0x" + "ab" * 32


def enc(types, values) -> str:
    return "0x" + encode(types, values).hex()


class Node:
    """A fake EVM node: eth_call answers keyed by (to, selector); logs are
    filtered like a real node unless `honor_address` is False."""

    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        self.calls: dict[tuple[str, str], object] = {}
        self.logs: list[dict] = []
        self.honor_address = True
        self.requests: list[dict] = []
        self.reject_override = False
        self.no_code: set[str] = set()
        self.head = 1000
        self.genesis_ts = 1_790_000_000
        self.storage: dict[tuple[str, str], str] = {}
        self.txs: dict[str, dict] = {}  # eth_getTransactionByHash
        self.nonces: dict[str, int] = {}  # eth_getTransactionCount (any block)

    def on(self, to: str, signature: str, result) -> None:
        self.calls[(to.lower(), "0x" + selector(signature).hex())] = result

    def handle(self, body: dict) -> dict:
        self.requests.append(body)
        m, p = body["method"], body.get("params") or []
        ok = lambda r: {"jsonrpc": "2.0", "id": body["id"], "result": r}  # noqa: E731
        err = lambda msg: {"jsonrpc": "2.0", "id": body["id"], "error": {"code": 3, "message": msg}}  # noqa: E731
        if m == "eth_chainId":
            return ok(hex(self.chain_id))
        if m == "eth_getCode":
            return ok("0x" if p[0].lower() in self.no_code else "0x6080604052")
        if m == "eth_blockNumber":
            return ok(hex(self.head))
        if m == "eth_getBlockByNumber":  # one block per second
            n = self.head if p[0] == "latest" else int(p[0], 16)
            return ok({"number": hex(n), "timestamp": hex(self.genesis_ts + n)})
        if m == "eth_getStorageAt":
            return ok(self.storage.get((p[0].lower(), p[1].lower()), "0x" + "00" * 32))
        if m == "eth_getLogs":
            f = p[0]
            addrs = f.get("address")
            addrs = {a.lower() for a in ([addrs] if isinstance(addrs, str) else addrs or [])}
            t0 = {t.lower() for t in f["topics"][0]} if f["topics"] and isinstance(f["topics"][0], list) \
                else {f["topics"][0].lower()} if f["topics"] else set()
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            out = [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi
                   and (not t0 or x["topics"][0].lower() in t0)
                   and (not self.honor_address or not addrs or x["address"].lower() in addrs)]
            if len(f["topics"]) > 1:
                out = [x for x in out if len(x["topics"]) > 1 and x["topics"][1].lower() == f["topics"][1].lower()]
            return ok(out)
        if m == "eth_getTransactionByHash":
            return ok(self.txs.get(p[0].lower()))
        if m == "eth_getTransactionCount":
            n = self.nonces.get(p[0].lower())
            return ok(hex(n)) if n is not None else err("method eth_getTransactionCount not faked for this address")
        if m == "eth_call":
            if len(p) > 2 and self.reject_override:
                return err("invalid params: too many arguments")
            key = (p[0]["to"].lower(), p[0]["data"][:10])
            r = self.calls.get(key)
            if r is None:
                return err("execution reverted")
            if isinstance(r, Exception):
                return err(str(r))
            return ok(r(p) if callable(r) else r)
        return err(f"method {m} not faked")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda req: httpx.Response(200, json=self.handle(json.loads(req.content))))


def rpc_for(node: Node, chain="bsc") -> EvmRpc:
    return EvmRpc(chain, node.chain_id, ["https://node.example/key-SECRET"],
                  client=httpx.AsyncClient(transport=node.transport()))


def log_of(ev, values, address, block=10, index=0) -> dict:
    return ev.encode_log(values, address, blockNumber=hex(block), logIndex=hex(index), transactionHash=TX)
