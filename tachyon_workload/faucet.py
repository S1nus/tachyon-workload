"""External transparent faucet: canonical reward accounting and bounded payouts."""

import hashlib

from .planner import FEE, available_notes

MATURITY = 100
SCAN_BATCH = 1000
MAX_MONEY = 21_000_000 * 100_000_000
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def transparent_script(address):
    """Decode only testnet P2PKH/P2SH, matching devtool's recipient rules."""
    if not isinstance(address, str) or not 26 <= len(address) <= 64:
        raise ValueError("faucet address must be a testnet transparent address")
    number = 0
    for char in address:
        if char not in BASE58:
            raise ValueError("invalid transparent address encoding")
        number = number * 58 + BASE58.index(char)
    raw = b"\0" * (len(address) - len(address.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(raw) != 26 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        raise ValueError("invalid transparent address checksum or length")
    prefix, payload = raw[:2], raw[2:-4].hex()
    if prefix == b"\x1d\x25":
        return "76a914" + payload + "88ac"
    if prefix == b"\x1c\xba":
        return "a914" + payload + "87"
    raise ValueError("faucet address must be testnet P2PKH or P2SH")


def validate_config(config):
    if not isinstance(config, dict) or set(config) - {
        "address", "reward_share_bps", "payout_interval_blocks", "min_payout_zat"
    }:
        raise ValueError("unknown or malformed faucet configuration")
    result = dict(reward_share_bps=1000, payout_interval_blocks=10, min_payout_zat=100_000) | config
    transparent_script(result.get("address"))
    for name, minimum, maximum in (("reward_share_bps", 1, 9999),
                                    ("payout_interval_blocks", 1, 1_000_000),
                                    ("min_payout_zat", FEE, MAX_MONEY)):
        if type(result[name]) is not int or not minimum <= result[name] <= maximum:
            raise ValueError(f"faucet.{name} must be an integer in {minimum}..{maximum}")
    return result


def _hash(value):
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError("faucet RPC returned an invalid hash")
    try:
        if len(bytes.fromhex(value)) != 32:
            raise ValueError("wrong hash length")
    except ValueError as error:
        raise RuntimeError("faucet RPC returned an invalid hash") from error
    return value.lower()


def _amount(value):
    if type(value) is not int or not 0 <= value <= MAX_MONEY:
        raise RuntimeError("faucet requires integer valueZat amounts within the monetary range")
    return value


class Faucet:
    def __init__(self, journal, config):
        self.journal = journal
        self.db = journal.db
        self.config = validate_config(config) if config is not None else None
        self.caught_up = False
        self.height = None
        # Additive migration: existing operations and wallet directories are unchanged.
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS faucet_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), start_height INTEGER NOT NULL,
                treasury_address TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS faucet_policies (
                id INTEGER PRIMARY KEY, start_height INTEGER NOT NULL,
                address TEXT, share_bps INTEGER NOT NULL, interval_blocks INTEGER NOT NULL,
                min_payout_zat INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS faucet_blocks (
                height INTEGER PRIMARY KEY, hash TEXT NOT NULL, policy_id INTEGER REFERENCES faucet_policies(id),
                reward_zat INTEGER NOT NULL, allocation_zat INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS faucet_operations (
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),
                policy_id INTEGER NOT NULL REFERENCES faucet_policies(id),
                amount_zat INTEGER NOT NULL, purpose TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS faucet_blocks_policy ON faucet_blocks(policy_id, height);
            CREATE INDEX IF NOT EXISTS faucet_operations_policy ON faucet_operations(policy_id);
        """)

    @property
    def tracked(self):
        return self.db.execute("SELECT 1 FROM faucet_settings").fetchone() is not None

    def initialize(self, height, block_hash, addresses):
        if self.config and self.config["address"] in {a["funding_address"] for a in addresses.values()}:
            raise ValueError("faucet must be external to the treasury and simulation wallets")
        if not self.config and not self.tracked:
            return
        treasury = addresses["treasury"]["funding_address"]
        transparent_script(treasury)
        existing = self.db.execute("SELECT * FROM faucet_settings").fetchone()
        if existing:
            if existing["treasury_address"] != treasury:
                raise RuntimeError("faucet treasury changed; preserve the original wallet")
            return
        with self.db:
            self.db.execute("INSERT INTO faucet_settings VALUES(1,?,?)", (height + 1, treasury))
            self.db.execute("INSERT INTO faucet_blocks VALUES(?,?,NULL,0,0)", (height, _hash(block_hash)))
            self._configure(height + 1)

    def _configure(self, start):
        desired = ((self.config["address"], self.config["reward_share_bps"],
                    self.config["payout_interval_blocks"], self.config["min_payout_zat"])
                   if self.config else (None, 0, 1, 1))
        last = self.db.execute("SELECT * FROM faucet_policies ORDER BY id DESC LIMIT 1").fetchone()
        if last and tuple(last[k] for k in ("address", "share_bps", "interval_blocks", "min_payout_zat")) == desired:
            return
        if last and last["start_height"] > start:
            raise RuntimeError("wait for the chain to recover before changing faucet policy")
        self.db.execute("INSERT INTO faucet_policies(start_height,address,share_bps,interval_blocks,min_payout_zat) "
                        "VALUES(?,?,?,?,?)", (start, *desired))

    def _policy(self, height):
        return self.db.execute("SELECT * FROM faucet_policies WHERE start_height<=? "
                               "ORDER BY start_height DESC,id DESC LIMIT 1", (height,)).fetchone()

    async def sync(self, rpc, height, tip_hash):
        """Replay a bounded canonical prefix, then enable payouts only at the wallet's tip."""
        self.caught_up = False
        self.height = height
        if not self.tracked:
            return True
        if _hash(await rpc.call("getblockhash", [height])) != _hash(tip_hash):
            raise RuntimeError("chain changed since treasury sync; retry before faucet payments")
        settings = self.db.execute("SELECT * FROM faucet_settings").fetchone()
        base = settings["start_height"] - 1
        if height < base:
            return False
        last = self.db.execute("SELECT * FROM faucet_blocks ORDER BY height DESC LIMIT 1").fetchone()
        upper = min(height, last["height"])

        async def canonical(h):
            return _hash(await rpc.call("getblockhash", [h]))

        async def matches(h):
            row = self.db.execute("SELECT hash FROM faucet_blocks WHERE height=?", (h,)).fetchone()
            return row["hash"] == await canonical(h)

        # Common-prefix binary search bounds RPC work even for a deep reorg.
        if await matches(upper):
            ancestor = upper
        elif not await matches(base):
            ancestor = base  # The baseline itself earns nothing; replace its anchor.
        else:
            low, high = base, upper
            while low < high:
                middle = (low + high + 1) // 2
                if await matches(middle):
                    low = middle
                else:
                    high = middle - 1
            ancestor = low
        parent = await canonical(ancestor)
        baseline_hash = parent if ancestor == base else None
        stop = min(height, ancestor + SCAN_BATCH)
        rows = []
        script = transparent_script(settings["treasury_address"])
        for h in range(ancestor + 1, stop + 1):
            block = await rpc.call("getblock", [str(h), 1])
            block_hash = _hash(block["hash"])
            if block["height"] != h or _hash(block["previousblockhash"]) != parent:
                raise RuntimeError("chain changed while scanning faucet rewards; retry")
            policy = self._policy(h)
            reward = 0
            if policy["address"]:
                if not block["tx"]:
                    raise RuntimeError("faucet block is missing its coinbase")
                txid = _hash(block["tx"][0])
                tx = await rpc.call("getrawtransaction", [txid, 1, block_hash])
                if (_hash(tx["txid"]) != txid or _hash(tx["blockhash"]) != block_hash
                        or len(tx["vin"]) != 1 or "coinbase" not in tx["vin"][0]):
                    raise RuntimeError("faucet coinbase identity mismatch")
                seen = set()
                for output in tx["vout"]:
                    index = output["n"]
                    if type(index) is not int or index < 0 or index in seen:
                        raise RuntimeError("faucet coinbase has invalid output indexes")
                    seen.add(index)
                    value = _amount(output["valueZat"])
                    if output["scriptPubKey"]["hex"].lower() == script:
                        reward = _amount(reward + value)
            allocation = reward * policy["share_bps"] // 10_000
            rows.append((h, block_hash, policy["id"], reward, allocation))
            parent = block_hash
        if await canonical(stop) != parent or await canonical(height) != _hash(tip_hash):
            raise RuntimeError("chain changed while scanning faucet rewards; retry")
        with self.db:
            self.db.execute("DELETE FROM faucet_blocks WHERE height>?", (ancestor,))
            if baseline_hash:
                self.db.execute("UPDATE faucet_blocks SET hash=? WHERE height=?", (baseline_hash, base))
            self.db.executemany("INSERT INTO faucet_blocks VALUES(?,?,?,?,?)", rows)
            if stop == height:
                # Catch up under the previous policy first. Edits apply only to
                # future rewards, never reprice or redirect existing allocations.
                self._configure(height + 1)
        self.caught_up = stop == height
        return self.caught_up

    def balances(self):
        if not self.tracked or self.height is None:
            return []
        policies = list(self.db.execute("SELECT * FROM faucet_policies ORDER BY start_height,id"))
        result = []
        mature_height = self.height - MATURITY + 1
        for index, policy in enumerate(policies):
            if not policy["address"]:
                continue
            start, interval = policy["start_height"], policy["interval_blocks"]
            end = policies[index + 1]["start_height"] - 1 if index + 1 < len(policies) else self.height
            mature_end = min(end, mature_height)
            cutoff = start + max(0, (mature_end - start + 1) // interval) * interval - 1
            if index + 1 < len(policies) and mature_height >= end:
                cutoff = end  # Flush the old policy's final, possibly short batch.
            sums = self.db.execute("SELECT COALESCE(SUM(allocation_zat),0), "
                "COALESCE(SUM(CASE WHEN height<=? THEN allocation_zat ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN height<=? THEN allocation_zat ELSE 0 END),0) "
                "FROM faucet_blocks WHERE policy_id=?", (mature_end, cutoff, policy["id"])).fetchone()
            paid, pending = self.db.execute("SELECT "
                "COALESCE(SUM(CASE WHEN o.status='mined' THEN f.amount_zat ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN o.status IN ('planned','pending') THEN f.amount_zat ELSE 0 END),0) "
                "FROM faucet_operations f JOIN operations o ON o.id=f.operation_id WHERE f.policy_id=?",
                (policy["id"],)).fetchone()
            result.append(dict(policy_id=policy["id"], address=policy["address"],
                start_height=start, reward_share_bps=policy["share_bps"], payout_interval_blocks=interval,
                allocated_zat=sums[0], immature_zat=sums[0] - sums[1], paid_zat=paid,
                in_flight_zat=pending, owed_zat=max(0, sums[1] - paid - pending),
                due_zat=max(0, sums[2] - paid - pending), credit_zat=max(0, paid - sums[1]),
                min_payout_zat=policy["min_payout_zat"]))
        # A reorg can leave an old policy overpaid even though later rewards use
        # a new rate. Carry credit only to the SAME recipient, never another one.
        for address in {row["address"] for row in result}:
            rows = [row for row in result if row["address"] == address]
            credit = sum(row["credit_zat"] for row in rows)
            for row in rows:
                used = min(credit, row["owed_zat"])
                row["owed_zat"] -= used
                row["due_zat"] = max(0, row["due_zat"] - used)
                credit -= used
            for row in rows:
                row["credit_zat"] = 0
            rows[-1]["credit_zat"] = credit
        return result

    def protected_zat(self):
        owed = sum(row["owed_zat"] for row in self.balances())
        return owed + FEE if owed else 0

    def payment(self, balances, utxos, busy, tachyon_allowed):
        """Return one treasury operation plus its atomic journal accounting tag."""
        if not self.config or not self.caught_up or "treasury" in busy or not tachyon_allowed:
            return None
        due = next((row for row in self.balances() if row["due_zat"] >= row["min_payout_zat"]), None)
        if due is None:
            return None
        capacity = max(available_notes(balances["treasury"]), default=0) - FEE
        amount = min(due["due_zat"], capacity)
        if amount >= due["min_payout_zat"]:
            return (("treasury", None, ["unshield", "--to", due["address"], "--amount-zat", str(amount)]),
                    (due["policy_id"], amount, "payout"))
        for utxo in utxos.get("treasury", []):
            if utxo["satoshis"] >= due["min_payout_zat"] + 2 * FEE:
                return (("treasury", None, ["shield", "--txid", utxo["txid"], "--index", str(utxo["outputIndex"])]),
                        (due["policy_id"], 0, "replenish"))
        return None

    def status(self):
        last = self.db.execute("SELECT MAX(height) FROM faucet_blocks").fetchone()[0]
        settings = self.db.execute("SELECT start_height FROM faucet_settings").fetchone()
        return dict(enabled=self.config is not None, caught_up=self.caught_up,
                    start_height=settings[0] if settings else None, scanned_height=last, policies=self.balances())
