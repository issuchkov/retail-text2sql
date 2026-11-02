"""Embed the golden trios into golden/index.json (SPEC §8 R1). Run after editing trios:

uv run python scripts/build_golden_index.py
"""

from __future__ import annotations

import json

from retail_agent.config import PROJECT_ROOT, load_env
from retail_agent.golden.library import (
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    GeminiEmbedder,
    build_index,
    load_trios,
)


def main() -> None:
    load_env()
    trios = load_trios(PROJECT_ROOT / "golden" / "trios")
    index = build_index(
        trios,
        GeminiEmbedder(EMBEDDING_MODEL, EMBEDDING_DIMS),
        model=EMBEDDING_MODEL,
        dims=EMBEDDING_DIMS,
    )
    path = PROJECT_ROOT / "golden" / "index.json"
    path.write_text(json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"indexed {len(trios)} trios with {EMBEDDING_MODEL} ({EMBEDDING_DIMS} dims) → {path}")


if __name__ == "__main__":
    main()
