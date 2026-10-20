# Tachyon workload sidecar

The Python 3.11+ Unix sidecar drives the existing JSON `tachyon` CLI. No Python
packages are required. Devtool retains all keys, signing, proof construction,
input reservations, and chain validation. This is **private testnet tooling with
mock proofs and unencrypted keys**, not a production wallet or proof service.

## Setup

The compatible devtool source revision is recorded in `DEVTOOL_REVISION`. Build its
`tachyon` feature as described in the
[wallet guide](https://github.com/S1nus/zcash-devtool/blob/0394ba0aeb95bd003f756bb073f3aa80f76279f2/doc/tachyon.md).
That pinned guide still mentions `ROCKSDB_LIB_DIR` and the removed
`tachyon_workload` mining option; omit both when following it.
The sidecar invokes the resulting binary; it does not import or modify devtool.
The pin includes bounded QR buckets; it is a source compatibility baseline, not
a claim that a node-backed smoke run has passed on your network. Check it out
explicitly in a clean checkout; its toolchain file selects Rust 1.97.1:

```sh
cd ../zcash-devtool
git checkout 0394ba0aeb95bd003f756bb073f3aa80f76279f2
CARGO_INCREMENTAL=0 \
  RUSTFLAGS='--cfg zcash_unstable="nutachyon"' \
  cargo build --release --locked --features tachyon
cd ../tachyon-workload
cp config.example.toml config.local.toml
```

Edit `config.local.toml` to set your RPC URL and paths. The example assumes a
sibling `zcash-devtool` checkout and stores private runtime data in a separate
sibling `tachyon-workload-data` directory, outside either source checkout.
Relative paths resolve against the config file, not the working directory.
Use a trusted RPC, preferably through an SSH tunnel. `cookie_file` is optional.

```sh
python3 -m tachyon_workload --config config.local.toml \
  --accept-test-funds init
```

This reuses `treasury` within `wallet_root`, or creates it if missing, and creates
the numbered
`sim-001` through `sim-032` wallets. It prints the treasury's `miner_address`.
Existing wallets must match the RPC's genesis and NuTachyon activation block.
Configure `mining.miner_address` with that address and use
`internal_miner = true` for live mode. The node revision used for this check,
`ec58f4bb797d2ad0390a157e175bc94c09ad979e`, rejects unknown mining
configuration fields. Adding the historical `tachyon_workload` option prevents
the node from starting. Wait 100 confirmations
for rewards. Do not reset the chain.

For a small smoke run, set `wallet_count = 4`, `max_transactions = 4`. Wallets
sync independently from activation; their initial synchronization over a remote
RPC can take several minutes. `concurrency` bounds simultaneous CLI commands.

```sh
# One funding payment per observed tip: full coinbase shielding, then note distribution.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  bootstrap --rounds 20
# Read-only chain activity: sync and report both pools without sending anything.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds status
# Draw a fresh transaction budget for each observed tip. Default is ONE round.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --rounds 10
# Explicitly opt into a continuous process; Ctrl-C preserves recovery state.
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --rounds 0
```

Bootstrap does not invent funds: it shields an entire mature reward minus the
50,000-zatoshi fee, then distributes differently sized Tachyon notes. Regular
rounds also refill underfunded wallets, **within** their budget. The default mix
is 80% intra-Tachyon, 8% unshield, 8% shield, and 4% transparent transfer, weighted
among currently affordable choices. Actual proportions differ during bootstrap
and when some wallets lack notes or transparent UTXOs. Receipt delivery/import
is automatic and private; there is no trial decryption or in-band secret delivery.

Each regular round draws uniformly from **zero through `max_transactions`**.
This counts all non-coinbase payments, including funding and transparent-only
payments, not only Tachyon transactions. Zero produces no new payment. A wallet
sends at most once per round, using confirmed inputs; outstanding transactions
block that wallet until resolved. Fees are additional to recipient amounts.
Unshielded funds can circulate transparently and then be reshielded.

## Mining and what the counts mean

**Live mode** works with your existing internal miner. Its budget is per
*observed tip*, not an exact transaction count in every mined block. Mining can
race construction, some tips can be skipped while syncing, and pending traffic
can carry into later blocks. A zero round does not promise a coinbase-only block.
Node-side aggregation remains Zakura's responsibility.

For deterministic batches on a PoW-disabled custom network, turn off
`internal_miner` and all other miners/producers, and use:

```sh
python3 -m tachyon_workload --config config.local.toml --accept-test-funds \
  run --mode controlled --sole-miner --rounds 10
```

Controlled mode requires an empty mempool before each new round, compares the
batch against the mempool, calls `generate(1)`, then verifies the actual block's
transaction IDs. A zero round gives a coinbase-only block under those conditions.
Insufficient independent inputs or an epoch boundary produces an explicit
`underfilled` event, not a false claim of hitting the sampled target. Concurrent
miners or unrelated mempool traffic fail closed. An ambiguous `generate` timeout
is recovered by inspecting the persisted round's successor before mining again.
If no successor is visible yet, it stops rather than guessing whether the server
is still processing the first request. Investigate the node before resuming.

The cap is a **load limit**, not the consensus maximum: block capacity depends on
serialized size, actions, and validation cost. Start small. Devtool currently
reconstructs proofs from local public inputs and limits closed epochs to 65,536
tachygrams. Sustained full-block stress testing needs scalable proof evidence,
more independent inputs, and throughput work beyond this initial sidecar.

## Durability and operations

The mode-0700 state directory contains a SQLite journal and confidential receipt
files. A process lock prevents two sidecars using the same journal. Each payment's
ID, arguments, sender and recipient are committed **before** invoking devtool.
Retries recover the same signed transaction; they never silently create a new ID.
Receipts use a staging file and atomic rename; imports are idempotent. Wallet sync
refreshes transaction states, including mined payments rolled back by reorgs.
Expired transactions are not blindly replaced. Errors stop the process with
state intact; fix the cause and rerun using the same config and journal.

Back up **both the complete wallet directories and the sidecar state**. Never
commit, log, or publicly serve receipts or SQLite files. The default paths are
outside the repository. Do not operate these wallets concurrently with another
sidecar/journal or manually spend their inputs. Do not reuse them after regenesis.
Config must retain wallets needed by pending payments. Increasing `wallet_count`
and rerunning `init` safely adds wallets. The scheduler's `--seed` affects only
traffic choices, never key generation; exact replay uses the journal, not a seed.

For systemd, run as the user owning the private directories, set `UMask=0077`, use
the repository as `WorkingDirectory`, and set `ExecStart` to the continuous command
with absolute Python/config paths. If using filesystem hardening, make only the
wallet/state paths writable. Run `init` first; do not install a restart loop until
the bounded smoke run succeeds.

### Existing wallets from the devtool checkout

Moving the sidecar code does not require moving or reinitializing wallets. Point
`devtool`, `wallet_root`, and `state_dir` in your ignored `config.local.toml` to
their existing absolute paths. Preserve the original `wallet_root` spelling:
the journal pins it as well as chain identity and wallet addresses. Do not create
a fresh journal for wallets with in-flight operations.

Alternatively, keep using the original config file; paths still resolve against
that file's directory. Replace the old `python3 -m tools.tachyon_workload` command
with `python3 -m tachyon_workload` and run it from this repository. No chain reset
or miner-address change is needed.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -v
```

Tests cover weighted selection, maturity, coinbase constraints, epoch boundaries,
zero rounds, private directories, journal locking and chain identity, reorg
recovery, ambiguous broadcasting/receipt delivery, and mining timeout recovery.

### CLI compatibility smoke

After building the pinned binary, check the command help and required arguments
used by the sidecar:

```sh
python3 -m tachyon_workload.compatibility ../zcash-devtool/target/release/zcash-devtool
```

This read-only check invokes help only: it never creates wallets or submits
transactions. It establishes CLI shape compatibility, not RPC or proof correctness.
No CLI-facing files changed between these pins; this is a guard for future changes.
It reports all command mismatches and timeouts together; an unavailable executable
stops the check immediately.
The pinned devtool already includes a regression that spends after an epoch with
more than 8,191 members. Run it in the devtool checkout to exercise the QR path:

```sh
RUSTFLAGS='--cfg zcash_unstable="nutachyon"' \
  cargo test --locked -p tachyon-wallet --lib \
  tests::spends_after_epoch_with_more_tachygrams_than_polynomial_capacity -- --exact
```

This uses the mock prover and does not measure production proving performance.
Node-backed small controlled rounds and epoch crossing remain a separate manual
integration check requiring an isolated compatible node. The Python test suite
runs without network, keys, a Rust build, or a node. The help check does not
verify the binary source revision; build the pinned checkout above.
