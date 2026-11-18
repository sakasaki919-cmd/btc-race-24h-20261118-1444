#!/usr/bin/env python3
"""Entrypoint for the BTC race bot (make.fun btc-race-24h-20261118-1444).

The grader runs ``python3 -P -s /bot/main.py`` with PYTHONHASHSEED=0: -P
leaves this directory off sys.path, so the vendored SDK (vendor/) and
the strategy package (src/) are added to it here, by absolute path.
The bot speaks agent-fun/bars-replay/v1 on stdin/stdout through the
vendored SDK shim (vendor/makefun_bars.py, a byte-identical copy of the
kit's sdk/makefun_bars.py). The strategy itself never touches stdin or
stdout: ``main`` just wires the SDK's ``run`` loop to the strategy's
``on_batch`` and lets the SDK own the protocol.

No network, no wall clock, no file reads or writes, no environment
reads and no randomness at runtime: every decision is a pure function
of the bars the harness has sent.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "vendor"))
sys.path.insert(0, _HERE)

import makefun_bars  # noqa: E402
from src.strategy import Strategy  # noqa: E402


def main():
    """Wire the SDK loop to the strategy and run until the harness
    sends ``done`` (end of data) or stdin closes."""
    strategy = Strategy()
    makefun_bars.run(strategy.on_batch)


if __name__ == "__main__":
    main()
