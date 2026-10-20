"""Read-only CLI surface smoke; no wallet directories or network calls."""

import argparse
import re
import subprocess

# Derived from Runner and planner invocations. Help is deliberately used instead
# of real payments: this check must be safe against an operator's existing node.
COMMANDS = {
    "init": ("--rpc-url", "--activation-height", "--cookie-file", "--accept-mock-proofs"),
    "address": (), "balance": (), "transactions": (),
    "sync": ("--max-blocks",), "operation": ("<ID>",),
    "send": ("--to", "--amount-zat", "--operation-id"),
    "unshield": ("--to", "--amount-zat", "--operation-id"),
    "transparent-send": ("--to", "--amount-zat", "--operation-id"),
    "shield": ("[AMOUNT_ZAT]", "--txid", "--index", "--operation-id"),
    "export-receipt": ("--txid", "--out"), "receive": ("--receipt",),
    "submit": ("--txid",),
}


def check(binary, run=subprocess.run):
    failures = []
    for command, tokens in COMMANDS.items():
        try:
            result = run([str(binary), "tachyon", "--wallet-dir", "/unused-help-only",
                          command, "--help"], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            failures.append(f"CLI help timed out for {command} after 30s")
            continue
        except OSError as error:
            raise RuntimeError(f"CLI help unavailable for {command}: {type(error).__name__}") from error
        if result.returncode:
            failures.append(f"CLI help failed for {command}: exit {result.returncode}")
            continue
        usage = next((line for line in result.stdout.splitlines()
                      if line.startswith("Usage:")), "")
        words = usage.split()
        if ("tachyon" not in words or command not in words
                or words.index(command) <= words.index("tachyon")
                or "--wallet-dir" not in words):
            failures.append(f"CLI {command} help has unexpected usage")
            continue
        missing = [token for token in tokens if (
            not re.search(r"^[ \t]+(?:-[^\s,-],[ \t]+)?" + re.escape(token) + r"(?=[\s=<]|$)",
                          result.stdout, re.MULTILINE)
            if token.startswith("--") else token not in usage
        )]
        if missing:
            failures.append(f"CLI {command} help missing: {', '.join(missing)}")
    if failures:
        raise RuntimeError("\n".join(failures))
    return len(COMMANDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    args = parser.parse_args()
    try:
        count = check(args.binary)
    except RuntimeError as error:
        parser.exit(1, f"Compatibility check failed: {error}\n")
    print(f"CLI shape compatible: {count} commands checked (help only)")


if __name__ == "__main__":
    main()
