# Third-party provenance

This repository is an independent research implementation, not an official Meteora, Solana or Hyperliquid product. Names are used for interoperability.

The integer pricing/quote semantics and compact binary layouts are based on the official MeteoraAg/dlmm-sdk files identified in `reference/source_lock.json`. The published SDK package declares ISC licensing (`ts-client/package.json`, pinned version 1.9.14). The account fixture in `tests/fixtures/official_lb_pair.json` is copied from the pinned official repository with its exact source/blob/account hashes recorded alongside it. Upstream attribution is preserved. This repository's MIT license applies to its new original implementation, not as a replacement for third-party rights.

The complete SDK/IDL/program binary are not bundled as if authored here. Optional source-sync downloads immutable official files for independent verification. Refer to the upstream repository/package for its full applicable licensing notices. No existing user Uniswap v3 code is copied or imported.
