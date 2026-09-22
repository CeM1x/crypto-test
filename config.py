import os

WALLET = os.environ.get("WALLET", "0x46b353667fd7d846af3bbeda6584b0e5b883d3de").lower()
WALLET_TOPIC = "0x" + "0" * 24 + WALLET[2:]

PG_DSN = os.environ.get("PG_DSN", "postgresql://polymarket:polymarket@localhost:5433/polymarket")

# Первый RPC - основной: нужен архивный узел и eth_getLogs по большим диапазонам
# Остальные - запасные, только для диапазонов <= 10 000 блоков
RPC_URLS = os.environ.get(
    "RPC_URLS",
    "https://polygon.gateway.tenderly.co,"
    "https://polygon-bor-rpc.publicnode.com,"
    "https://rpc-mainnet.matic.quiknode.pro",
).split(",")
FALLBACK_MAX_SPAN = 10_000

CHUNK_BLOCKS = 250_000   # размер задания для воркера
WORKERS = 4

NATIVE_TOKEN = "0x0000000000000000000000000000000000001010"  # POL (системный контракт Polygon)

CONTRACTS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC.e",
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "pUSD (Polymarket USD)",
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045": "ConditionalTokens (CTF)",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e": "CTF Exchange v1",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a": "NegRisk CTF Exchange v1",
    "0xe111180000d2663c0091e4f400237545b87b996b": "CTF Exchange v2",
    "0xe2222d279d744050d28e00520010520000310f59": "NegRisk CTF Exchange v2",
    "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296": "NegRisk Adapter",
    "0xf3cfb6a6ebfeb51876289eb235719eb1c65252b0": "pUSD CTF adapter",
    "0xa1200000d0002264c9a1698e001292d00e1b00af": "pUSD NegRisk adapter",
    "0xe3f18acc55091e2c48d883fc8c8413319d4ab7b0": "Fee Module (CTF Exchange)",
    "0xb768891e3130f6df18214ac804d4db76c2c37730": "Fee Module (NegRisk Exchange)",
    NATIVE_TOKEN: "POL (native)",
}

# Сигнатуры событий; topic0 вычисляется как keccak256 в db.seed()
EVENTS = [
    "Transfer(address,address,uint256)",
    "Approval(address,address,uint256)",
    "TransferSingle(address,address,address,uint256,uint256)",
    "TransferBatch(address,address,address,uint256[],uint256[])",
    "ApprovalForAll(address,address,bool)",
    "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
    "OrdersMatched(bytes32,address,uint256,uint256,uint256,uint256)",
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)",
    "OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)",
    "OrderCancelled(bytes32)",
    "PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)",
    "PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)",
    "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)",
    "PayoutRedemption(address,bytes32,uint256[],uint256)",
    "PositionSplit(address,bytes32,uint256)",
    "PositionsMerge(address,bytes32,uint256)",
    "PositionsConverted(address,bytes32,uint256,uint256)",
    "FeeRefunded(bytes32,address,uint256,uint256,uint256)",
    "Wrapped(address,address,address,uint256)",
    "Unwrapped(address,address,address,uint256)",
    "LogTransfer(address,address,address,uint256,uint256,uint256,uint256,uint256)",
    "LogFeeTransfer(address,address,address,uint256,uint256,uint256,uint256,uint256)",
]

# Активы, которые сверяются с блокчейном ВСЕГДА, даже если в загруженных логах их нет
# (расчётный баланс тогда 0, и on-chain обязан быть 0). Защита от полностью потерянного актива
CORE_ASSETS = {
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "erc20",   # USDC.e
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "erc20",   # USDC
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": "erc20",   # pUSD
    NATIVE_TOKEN: "native",
}
