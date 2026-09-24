-- Сырые логи Polygon, в которых кошелёк встречается в indexed-топиках 1..3.
CREATE TABLE IF NOT EXISTS raw_logs (
    block_number bigint      NOT NULL,
    log_index    integer     NOT NULL,
    block_time   timestamptz,
    block_hash   bytea       NOT NULL,
    tx_hash      bytea       NOT NULL,
    tx_index     integer     NOT NULL,
    address      bytea       NOT NULL,
    topic0       bytea,
    topic1       bytea,
    topic2       bytea,
    topic3       bytea,
    data         bytea       NOT NULL,
    PRIMARY KEY (block_number, log_index)
);
CREATE INDEX IF NOT EXISTS raw_logs_addr_topic0 ON raw_logs (address, topic0);
CREATE INDEX IF NOT EXISTS raw_logs_tx ON raw_logs (tx_hash);

-- План/прогресс сканирования: диапазон блоков на каждую позицию топика.
CREATE TABLE IF NOT EXISTS scan_chunks (
    topic_pos  smallint NOT NULL,
    from_block bigint   NOT NULL,
    to_block   bigint   NOT NULL,
    done       boolean  NOT NULL DEFAULT false,
    logs_found integer  NOT NULL DEFAULT 0,
    PRIMARY KEY (topic_pos, from_block)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    address bytea PRIMARY KEY,
    label   text NOT NULL
);

CREATE TABLE IF NOT EXISTS event_signatures (
    topic0    bytea PRIMARY KEY,
    name      text NOT NULL,
    signature text NOT NULL
);

-- Все изменения балансов кошелька (одна строка = одно движение одного актива).
CREATE TABLE IF NOT EXISTS ledger (
    block_number bigint         NOT NULL,
    log_index    integer        NOT NULL,
    sub_index    integer        NOT NULL,  -- позиция внутри TransferBatch / self-transfer
    block_time   timestamptz,
    tx_hash      bytea          NOT NULL,
    standard     text           NOT NULL,  -- erc20 | erc721 | erc1155 | native
    token        bytea          NOT NULL,
    token_id     numeric(78, 0) NOT NULL DEFAULT 0,  -- 0 для erc20/native
    delta        numeric(78, 0) NOT NULL,
    counterparty bytea          NOT NULL,
    PRIMARY KEY (block_number, log_index, sub_index)
);
CREATE INDEX IF NOT EXISTS ledger_asset ON ledger (token, token_id);
CREATE INDEX IF NOT EXISTS ledger_tx ON ledger (tx_hash);

-- Исполненные ордера кошелька (строки OrderFilled, где maker = кошелёк).
CREATE TABLE IF NOT EXISTS order_fills (
    block_number bigint         NOT NULL,
    log_index    integer        NOT NULL,
    block_time   timestamptz,
    tx_hash      bytea          NOT NULL,
    exchange     bytea          NOT NULL,
    version      smallint       NOT NULL,
    order_hash   bytea          NOT NULL,
    counterparty bytea          NOT NULL,
    side         text           NOT NULL,  -- BUY | SELL (с точки зрения кошелька)
    token_id     numeric(78, 0) NOT NULL,
    shares       numeric(78, 0) NOT NULL,
    usd          numeric(78, 0) NOT NULL,
    fee          numeric(78, 0) NOT NULL,  -- как в событии; в v1 при BUY комиссия выражена в акциях, не в USD
    PRIMARY KEY (block_number, log_index)
);

-- Результат сверки с блокчейном.
CREATE TABLE IF NOT EXISTS balance_check (
    checked_block bigint         NOT NULL,
    standard      text           NOT NULL,
    token         bytea          NOT NULL,
    token_id      numeric(78, 0) NOT NULL,
    computed      numeric(78, 0) NOT NULL,
    onchain       numeric(78, 0),
    ok            boolean        NOT NULL,
    note          text,
    PRIMARY KEY (checked_block, standard, token, token_id)
);
-- Миграция: раньше ключ не включал standard, и контракт, эмитящий и Transfer, и TransferSingle
-- (типичный спам-токен), ронял сверку на duplicate key.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
               WHERE c.conname = 'balance_check_pkey' GROUP BY c.oid HAVING NOT bool_or(a.attname = 'standard')) THEN
        ALTER TABLE balance_check DROP CONSTRAINT balance_check_pkey;
        ALTER TABLE balance_check ADD PRIMARY KEY (checked_block, standard, token, token_id);
    END IF;
END $$;

CREATE OR REPLACE VIEW balances AS
SELECT standard,
       '0x' || encode(token, 'hex') AS token,
       c.label,
       token_id,
       sum(delta)  AS balance,
       count(*)    AS movements,
       min(block_time) AS first_seen,
       max(block_time) AS last_seen
FROM ledger l
LEFT JOIN contracts c ON c.address = l.token
GROUP BY standard, token, c.label, token_id;

-- Человекочитаемая лента всех событий.
CREATE OR REPLACE VIEW events AS
SELECT r.block_number, r.log_index, r.block_time,
       '0x' || encode(r.tx_hash, 'hex') AS tx_hash,
       '0x' || encode(r.address, 'hex') AS contract,
       c.label AS contract_label,
       coalesce(s.name, '0x' || encode(r.topic0, 'hex')) AS event
FROM raw_logs r
LEFT JOIN contracts c ON c.address = r.address
LEFT JOIN event_signatures s ON s.topic0 = r.topic0;

-- Сводка по транзакциям: изменение USD (USDC.e + pUSD), кол-во движений позиций, события.
CREATE OR REPLACE VIEW tx_summary AS
WITH l AS (
    SELECT tx_hash, min(block_number) AS block_number, min(block_time) AS block_time,
           sum(delta) FILTER (WHERE standard = 'erc20' AND token IN
               ('\x2791bca1f2de4661ed88a30c99a7a9449aa84174', '\xc011a7e12a19f7b1f670d46f03b03f3342e82dfb'))
               / 1e6 AS usd_delta,
           count(*) FILTER (WHERE standard = 'erc1155') AS position_moves
    FROM ledger GROUP BY tx_hash
), e AS (
    SELECT r.tx_hash, array_agg(DISTINCT coalesce(s.name, 'unknown')) AS events
    FROM raw_logs r LEFT JOIN event_signatures s ON s.topic0 = r.topic0
    GROUP BY r.tx_hash
)
SELECT l.block_number, l.block_time, '0x' || encode(l.tx_hash, 'hex') AS tx_hash,
       coalesce(l.usd_delta, 0) AS usd_delta, l.position_moves, e.events
FROM l JOIN e USING (tx_hash);
