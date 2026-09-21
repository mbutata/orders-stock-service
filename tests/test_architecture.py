"""AC-ARCH-01: the component dependency rules hold."""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each row of the dependency rules in specs/03-architecture.md, and the contracts encoding it.
RULES = {
    "kernel: standard library and third-party packages only": {"kernel", "kernel-independent"},
    "catalog: the kernel": {"catalog", "catalog-private"},
    "orders: the catalog public interface and the kernel": {"orders", "orders-private"},
    "inventory: the orders public interface and the kernel": {"inventory", "inventory-private"},
    "api: orders, inventory and the kernel": {"api"},
    "demo.seed_data: the catalog public interface": {"demo-seed-data"},
    "demo.seed: seed_data, catalog, inventory and the kernel": {"demo-seed"},
    "demo.burst, demo.feed_consumer: config and httpx only": {
        "demo-http-clients",
        "demo-http-clients-independent",
    },
    "only cli imports api, and nothing imports cli": {"api-importers", "cli-importers"},
}


def test_ac_arch_01_component_dependency_rules_hold() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {contract["id"] for contract in config["tool"]["importlinter"]["contracts"]}
    for rule, ids in RULES.items():
        assert ids <= contracts, f"rule {rule!r} is not encoded: missing {ids - contracts}"

    result = subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Contracts: {len(contracts)} kept, 0 broken." in result.stdout
