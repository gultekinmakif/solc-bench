"""Extract real-world contracts from Sourcify

growthepie:    https://api.growthepie.xyz/v1/top_contracts/export_ethereum.json
Sourcify v2:   https://sourcify.dev/server/api-docs/swagger.json
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GROWTHEPIE_URL = "https://api.growthepie.xyz/v1/top_contracts/export_ethereum.json"
SOURCIFY_API = "https://sourcify.dev/server/v2/contract"
MAINNET_CHAIN_ID = 1
# Metadata responses are single-digit KB
# stdJsonInput for big multi-file contracts (USDC, Aave, ...) can be tens of KB and occasionally slow.
METADATA_TIMEOUT = 15
SOURCES_TIMEOUT = 60
HTTP_RETRIES = 3


@dataclass
class _BenchEntry:
    standard_json: dict
    bench_id: str
    compiler_version: str
    implementation_address: str | None
    compilation_metadata: dict


def _version_key(version):
    # Strip '+commit.<hash>'. Lexical compare gets '0.8.5' >= '0.8.20' wrong.
    parts = version.split("+")[0].split(".")
    if len(parts) != 3:
        raise ValueError(f"expected MAJOR.MINOR.PATCH, got {version!r}")
    major, minor, patch = (int(p) for p in parts)
    return major * 1_000_000 + minor * 1_000 + patch


def _get_json(url, timeout):
    """GET with retries on transient errors. Raises on permanent failure."""
    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last_error = e
        except (TimeoutError, urllib.error.URLError) as e:
            last_error = e
        if attempt < HTTP_RETRIES - 1:
            time.sleep(2 ** attempt)
    raise last_error


def _fetch_top_addresses(top_n):
    print(f"Fetching top-{top_n} mainnet contracts from growthepie...", file=sys.stderr)
    items = _get_json(GROWTHEPIE_URL, METADATA_TIMEOUT)
    return [
        {
            "address": item["address"].lower(),
            "name": item.get("name") or "",
            "owner_project": item.get("owner_project") or "",
            "usage_category": item.get("usage_category") or "",
            "tx_count": int(item.get("txcount_180d") or 0),
        }
        for item in items[:top_n]
    ]


def _fetch_metadata(address):
    url = f"{SOURCIFY_API}/{MAINNET_CHAIN_ID}/{address}?fields=compilation,proxyResolution"
    try:
        return _get_json(url, METADATA_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _fetch_standard_json(address):
    url = f"{SOURCIFY_API}/{MAINNET_CHAIN_ID}/{address}?fields=stdJsonInput"
    try:
        return _get_json(url, SOURCES_TIMEOUT).get("stdJsonInput")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _resolve_proxy(metadata):
    proxy = metadata.get("proxyResolution") or {}
    if not proxy.get("isProxy"):
        return None
    implementations = proxy.get("implementations") or []
    if not implementations:
        return None
    return implementations[0]["address"].lower()


def _check_version(compilation_metadata, min_version_key):
    compiler_version = compilation_metadata.get("compilerVersion", "")
    try:
        if _version_key(compiler_version) < min_version_key:
            return f"solc {compiler_version}"
    except ValueError:
        return f"unparseable solc version {compiler_version!r}"
    return None


def _patch_settings(standard_json):
    # Force outputs we measure on. Metadata stripping happens at run time
    # via solidity.resolve_solc_settings.
    settings = standard_json.setdefault("settings", {})
    settings.setdefault(
        "outputSelection",
        {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}},
    )
    return standard_json


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_bench_id(name, address):
    # Last 8 chars, not first: vanity addresses (e.g. Seaport)
    # collide with other contracts with 00000000 on a head slice.
    safe_name = _SAFE_NAME.sub("_", name) if name else "contract"
    return f"{safe_name}-{address[-8:]}"


def _toml_str(value):
    # TOML basic strings share JSON's escape rules
    # stdlib-only and safe for arbitrary user-provided contract names.
    return json.dumps(value, ensure_ascii=False)


def _toml_entry(entry, address_entry, original_address):
    tags = ["sourcify", "mainnet"]
    if entry.implementation_address:
        tags.append("proxy")

    # TODO: add ir-ssacfg
    fields = [
        f'pipelines = ["evmasm", "ir"]',
        f'gas = false',
        f'tags = [{", ".join(_toml_str(t) for t in tags)}]',
        f'sourcify_version = {_toml_str(entry.compiler_version)}',
        f'sourcify_fqn = {_toml_str(entry.compilation_metadata.get("fullyQualifiedName", ""))}',
        f'mainnet_address = {_toml_str(entry.implementation_address or original_address)}',
    ]
    if entry.implementation_address:
        fields.append(f'proxy_address = {_toml_str(original_address)}')
    fields.extend([
        f'tx_count_180d = {address_entry["tx_count"]}',
        f'name = {_toml_str(address_entry["name"])}',
        f'owner_project = {_toml_str(address_entry["owner_project"])}',
        f'usage_category = {_toml_str(address_entry["usage_category"])}',
    ])
    body = "\n".join(fields)
    return f'[{_toml_str(entry.bench_id)}]\n{body}\n'


def _process_contract(address_entry, address, min_version_key):
    """Fetch + filter one contract. Return _BenchEntry or None."""
    metadata = _fetch_metadata(address)
    if metadata is None:
        print("    skip: not verified on Sourcify", file=sys.stderr)
        return None

    compilation_metadata = metadata.get("compilation") or {}
    skip_reason = _check_version(compilation_metadata, min_version_key)
    if skip_reason:
        print(f"    skip: {skip_reason}", file=sys.stderr)
        return None

    implementation_address = _resolve_proxy(metadata)
    if implementation_address is not None:
        metadata = _fetch_metadata(implementation_address)
        if metadata is None:
            print(
                f"    skip: proxy impl {implementation_address} not verified",
                file=sys.stderr,
            )
            return None
        compilation_metadata = metadata.get("compilation") or {}
        skip_reason = _check_version(compilation_metadata, min_version_key)
        if skip_reason:
            print(f"    skip (impl): {skip_reason}", file=sys.stderr)
            return None

    bench_address = implementation_address or address

    if compilation_metadata.get("language") != "Solidity":
        print(
            f"    skip: language={compilation_metadata.get('language')!r}",
            file=sys.stderr,
        )
        return None

    standard_json = _fetch_standard_json(bench_address)
    if not standard_json:
        print("    skip: no stdJsonInput in response", file=sys.stderr)
        return None

    return _BenchEntry(
        standard_json=_patch_settings(standard_json),
        bench_id=_safe_bench_id(address_entry["name"], bench_address),
        compiler_version=compilation_metadata.get("compilerVersion", ""),
        implementation_address=implementation_address,
        compilation_metadata=compilation_metadata,
    )


def extract(output_dir, top_n=25, min_version="0.8.0", force=False):
    """Extract the top-N most-used mainnet Solidity contracts as a bench suite."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        if not force:
            raise FileExistsError(
                f"output dir is not empty: {output_dir} "
                "(pass --force to overwrite, or choose a different --output-dir)"
            )
        for child in output_dir.iterdir():
            if child.is_file():
                child.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    min_version_key = _version_key(min_version)

    address_entries = _fetch_top_addresses(top_n)
    print(
        f"Looking up {len(address_entries)} contracts on Sourcify "
        f"(mainnet, solc >= {min_version})...",
        file=sys.stderr,
    )

    toml_entries = []
    matched_count = 0
    for index, address_entry in enumerate(address_entries, start=1):
        address = address_entry["address"]
        print(
            f"  [{index}/{len(address_entries)}] {address} {address_entry['name']}",
            file=sys.stderr,
        )

        try:
            entry = _process_contract(address_entry, address, min_version_key)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"    skip: HTTP error after retries ({e})", file=sys.stderr)
            continue

        if entry is None:
            continue

        (output_dir / f"{entry.bench_id}.json").write_text(
            json.dumps(entry.standard_json, indent=2)
        )
        toml_entries.append(_toml_entry(entry, address_entry, address))

        matched_count += 1
        proxy_note = f" via proxy {address}" if entry.implementation_address else ""
        print(
            f"    matched: solc {entry.compiler_version}{proxy_note}",
            file=sys.stderr,
        )

    (output_dir / "benchmarks.toml").write_text("\n".join(toml_entries))
    print(
        f"Wrote {matched_count} of {len(address_entries)} contracts to {output_dir}",
        file=sys.stderr,
    )
    return matched_count
