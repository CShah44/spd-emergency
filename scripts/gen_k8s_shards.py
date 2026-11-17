"""Regenerate k8s/storage-shard1.yaml and storage-shard2.yaml from storage-shard0.yaml.

    python scripts/gen_k8s_shards.py

Edit shard 0 only, then run this, so the three files never drift apart.
"""

import re
from pathlib import Path

K8S = Path(__file__).resolve().parent.parent / "k8s"


def main() -> None:
    template = (K8S / "storage-shard0.yaml").read_text(encoding="utf-8")
    for shard in (1, 2):
        text = re.sub(r"\bshard0\b", f"shard{shard}", template)   # names, DNS, PVCs
        text = text.replace('shard: "0"', f'shard: "{shard}"')    # labels/selectors
        text = text.replace('value: "0"', f'value: "{shard}"')    # SHARD_ID
        text = re.sub(r"# \[A\] Shard 0:.*?\n(#.*\n)*", f"# [A] Shard {shard}. GENERATED from storage-shard0.yaml by scripts/gen_k8s_shards.py; edit that file instead.\n", text, count=1)
        # newline="\n": keep LF on Windows too (see .gitattributes)
        (K8S / f"storage-shard{shard}.yaml").write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote k8s/storage-shard{shard}.yaml")


if __name__ == "__main__":
    main()
