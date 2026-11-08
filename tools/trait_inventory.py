"""List everything SPAN's cloud tells the app about an account's panels.

The integration decodes three of the traits the app subscribes to: circuit names,
panel spaces, and relay state. The app shows much more -- PowerUp, always-on
breakers, backup priority, network links, alerts -- and some of it may already be
in the same snapshot. This records all of it, so each feature can be traced to
the trait and field that carries it.

    .venv/bin/python tools/trait_inventory.py

For every trait on every panel it records how many instances there are, which
messages in the recovered app schema (docs/reference/span-cloud-schema.txt) its
shape fits, and every value of every instance. Per-breaker instances are labelled
with their panel space and breaker size, so they can be matched against the app.
It also reads the app's feed (ListDispatches) and, where the snapshot shows it,
the PowerUp history.

Read-only: only the read calls listed in READ_CALLS can be made, and the stream
runs on fresh device ids, so Home Assistant's stream is left alone. The login is
prompted for and kept in memory. Strings are replaced with placeholders except
constant-looking ones (UPPER_CASE) and model codes; numbers are kept, since the
point is to see them. Read the report before sharing it.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import struct
import sys
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "span_ebus"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_panels import Recorder, Redactor, _listen  # noqa: E402
from span_client import backend, cloud_ably, cloud_auth, cloud_grpc  # noqa: E402
from span_client.cloud_pb import (  # noqa: E402
    ProtoError,
    field_message,
    field_string,
    field_varint,
    iter_fields,
    parse,
)
from span_client.cloud_traits import CircuitInfo, parse_trait_snapshot  # noqa: E402

SCHEMA = ROOT / "docs" / "reference" / "span-cloud-schema.txt"
ATTACH_SECONDS = 3
CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# The only calls this tool can make. Everything else on the service writes, or
# triggers something, and is refused before it is sent.
READ_CALLS = frozenset(
    {
        "GetSitesForUser",
        "GetResourcesForHardware",
        "AblyToken",
        "SubscribeAndGetTraits",
        "ListLatestTraitData",
        "ListLatestStates",
        "ListDispatches",
        "GetPowerUpHistory",
    }
)

# Beyond the app's 31: every trait id up to 64 in SPAN's namespace (vendor 1) and a
# few in the common one (vendor 5), to find traits the app does not ask for.
WIDE_TRAITS = tuple((1, n) for n in range(1, 65)) + tuple((5, n) for n in range(1, 9))

_WIRE_FOR = {
    0: {"int64", "uint64", "int32", "uint32", "bool", "enum", "sint32", "sint64"},
    1: {"double", "fixed64", "sfixed64"},
    2: {"string", "bytes", "message", "map"},
    5: {"float", "fixed32", "sfixed32"},
}


class ReadOnlyGrpc:
    """A cloud client that can only make the calls in READ_CALLS."""

    def __init__(self, inner: cloud_grpc.CloudGrpcClient) -> None:
        self._inner = inner

    def call(self, method: str, request: bytes = b"") -> bytes:
        if method not in READ_CALLS:
            raise PermissionError(f"{method} is not a read call; refused")
        return self._inner.call(method, request)

    def get_sites_for_user(self) -> bytes:
        return self.call("GetSitesForUser")

    def get_resources_for_hardware(self, request: bytes) -> bytes:
        return self.call("GetResourcesForHardware", request)

    def ably_token(self, request: bytes) -> bytes:
        return self.call("AblyToken", request)


# --- the app schema -------------------------------------------------------------


@dataclass
class SchemaMessage:
    name: str
    fields: dict[int, tuple[str, str]] = field(default_factory=dict)  # no -> (name, type)


def load_schema(path: Path) -> list[SchemaMessage]:
    messages: list[SchemaMessage] = []
    current: SchemaMessage | None = None
    for line in path.read_text().splitlines():
        if line.startswith("io."):
            current = SchemaMessage(line.strip())
            messages.append(current)
            continue
        match = re.match(r"\s+(\d+): (\w+) = (\w+)", line)
        if match and current is not None:
            current.fields[int(match[1])] = (match[2], match[3])
    return [m for m in messages if m.fields]


def _compatible(wire: int, kind: str) -> bool:
    # A length-delimited field can also be a packed repeated number.
    return kind in _WIRE_FOR.get(wire, set()) or wire == 2


def candidates(seen: dict[int, set[int]], schema: list[SchemaMessage], top: int = 3):
    """Schema messages every observed field fits, best coverage first."""
    ranked = []
    for message in schema:
        if not all(
            no in message.fields and all(_compatible(w, message.fields[no][1]) for w in wires)
            for no, wires in seen.items()
        ):
            continue
        exact = sum(
            all(message.fields[no][1] in _WIRE_FOR.get(w, set()) for w in wires)
            for no, wires in seen.items()
        )
        coverage = len(seen) / len(message.fields)
        is_trait = message.name.endswith("Trait")
        ranked.append((is_trait, exact, coverage, message))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    return [(r[3], r[2]) for r in ranked[:top]]


# --- decoding -------------------------------------------------------------------


def flatten(raw: bytes, redact: Redactor, prefix: str = "", depth: int = 0) -> list[str]:
    """Every leaf as `path=value`; nested messages are walked, strings redacted."""
    out: list[str] = []
    try:
        fields = list(iter_fields(raw))
    except (ProtoError, ValueError, IndexError):
        return [f"{prefix or '.'}=<bytes:{len(raw)}>"]
    for no, wire, value in fields:
        path = f"{prefix}.{no}" if prefix else str(no)
        if wire == 0:
            out.append(f"{path}={value}")
        elif wire == 5:
            out.append(f"{path}={struct.unpack('<f', value.to_bytes(4, 'little'))[0]:g}f")
        elif wire == 1:
            out.append(f"{path}={struct.unpack('<d', value.to_bytes(8, 'little'))[0]:g}d")
        else:
            out.extend(_flatten_len(path, value, redact, depth))
    return out


def _flatten_len(path: str, value: bytes, redact: Redactor, depth: int) -> list[str]:
    try:
        text = value.decode()
    except UnicodeDecodeError:
        text = None
    if text is not None and text and text.isprintable():
        shown = text if CONSTANT.match(text) else redact.text(text)
        # A short printable run can also be a tiny message; say both when unsure.
        if depth < 12 and len(value) <= 4:
            nested = flatten(value, redact, path, depth + 1)
            if nested and not any("<bytes" in n for n in nested):
                return [f"{path}={shown!s} | as message: {' '.join(nested)}"]
        return [f"{path}={shown}"]
    if not value:
        return [f"{path}=<empty>"]
    if depth < 12:
        return flatten(value, redact, path, depth + 1)
    return [f"{path}=<bytes:{len(value)}>"]


def top_level_wires(raw: bytes) -> dict[int, set[int]]:
    seen: dict[int, set[int]] = defaultdict(set)
    try:
        for no, wire, _ in iter_fields(raw):
            seen[no].add(wire)
    except (ProtoError, ValueError, IndexError):
        pass
    return seen


@dataclass
class TraitEntry:
    vendor: int
    trait: int
    instance: int | None
    value: bytes


def snapshot_entries(snapshot: bytes) -> dict[str, list[TraitEntry]]:
    """Trait entries per resource (hardware id), in snapshot order."""
    out: dict[str, list[TraitEntry]] = {}
    for block in parse(snapshot).values(1):
        message = parse(block)
        ref = message.get_msg_opt(1)
        resource = (ref.get_str(1) if ref is not None else None) or ""
        entries = out.setdefault(resource, [])
        for entry in message.get_msgs(2):
            key = entry.get_msg_opt(1)
            inst = entry.get_msg_opt(2)
            body = entry.get_msg_opt(3)
            value = b""
            if body is not None:
                raw = body.values(2)
                value = raw[0] if raw and isinstance(raw[0], bytes) else b""
            entries.append(
                TraitEntry(
                    vendor=(key.get_int_opt(1) if key else None) or 0,
                    trait=(key.get_int_opt(3) if key else None) or 0,
                    instance=inst.get_int_opt(1) if inst else None,
                    value=value,
                )
            )
    return out


def subscribe_request(channel: str, hardware_ids, traits) -> bytes:
    pairs = b"".join(
        field_message(2, field_varint(1, vendor) + field_varint(3, trait))
        for vendor, trait in traits
    )
    body = field_message(1, field_string(1, channel))
    for hardware_id in hardware_ids:
        body += field_message(2, field_message(1, field_string(1, hardware_id)) + pairs)
    return body


def latest_request(hardware_ids, traits) -> bytes:
    """ListLatestTraitDataRequest: the subscribe request's resources, at field 1."""
    pairs = b"".join(
        field_message(2, field_varint(1, vendor) + field_varint(3, trait))
        for vendor, trait in traits
    )
    return b"".join(
        field_message(1, field_message(1, field_string(1, hw)) + pairs) for hw in hardware_ids
    )


# --- the report -----------------------------------------------------------------


def trait_lines(
    entries: list[TraitEntry],
    circuits: dict[int, CircuitInfo],
    schema: list[SchemaMessage],
    redact: Redactor,
) -> list[str]:
    by_trait: dict[tuple[int, int], list[TraitEntry]] = defaultdict(list)
    for entry in entries:
        by_trait[(entry.vendor, entry.trait)].append(entry)
    lines: list[str] = []
    for (vendor, trait), group in sorted(by_trait.items()):
        seen: dict[int, set[int]] = defaultdict(set)
        for entry in group:
            for no, wires in top_level_wires(entry.value).items():
                seen[no] |= wires
        fits = candidates(seen, schema)
        best = fits[0][0] if fits else None
        lines.append("")
        lines.append(f"-- trait {vendor}/{trait}: {len(group)} instance(s)")
        lines.append(
            "   shape fits (a guess): "
            + (
                ", ".join(
                    f"{m.name.rsplit('.', 2)[-2]}.{m.name.rsplit('.', 1)[-1]} ({cov:.0%})"
                    for m, cov in fits
                )
                or "nothing in the schema"
            )
        )
        if best is not None:
            named = ", ".join(
                f"{no}={best.fields[no][0]}" for no in sorted(seen) if no in best.fields
            )
            lines.append(f"   fields (as {best.name.rsplit('.', 1)[-1]}): {named}")
        for entry in group:
            where = ""
            info = circuits.get(entry.instance or -1)
            if info is not None:
                spaces = "/".join(str(s) for s in info.spaces) or "?"
                where = f" [space {spaces}, {info.breaker_amps or '?'} A]"
            leaves = flatten(entry.value, redact) if entry.value else ["<no value>"]
            lines.append(f"   #{entry.instance}{where}: {'  '.join(leaves)}")
    return lines


def dispatch_lines(raw: bytes, redact: Redactor) -> list[str]:
    response = parse(raw)
    dispatches = response.values(1)
    lines = [f"{len(dispatches)} dispatch(es)"]
    kinds: dict[str, int] = defaultdict(int)
    for item in dispatches:
        if not isinstance(item, bytes):
            continue
        fields = sorted({no for no, _, _ in iter_fields(item)})
        kinds[",".join(map(str, fields))] += 1
        lines.append("   " + "  ".join(flatten(item, redact)))
    lines.insert(
        1, "   top-level field sets: " + "; ".join(f"[{k}] x{n}" for k, n in kinds.items())
    )
    return lines


def inventory(email: str, password: str, redact: Redactor, *, wide: bool) -> str:
    schema = load_schema(SCHEMA)
    redact.text(email)
    tokens = cloud_auth.authenticate(email, password)
    user_id = cloud_auth.user_id_from_token(tokens.access_token)
    if user_id:
        redact.text(user_id)
    stop = threading.Event()
    errors: list[str] = []
    lines: list[str] = [f"schema: {len(schema)} messages"]

    with cloud_grpc.CloudGrpcClient(tokens.access_token) as inner:
        grpc = ReadOnlyGrpc(inner)
        sites = backend.pair_by_lookup(
            backend.parse_sites(grpc.get_sites_for_user()), grpc.get_resources_for_hardware
        )
        for number, site in enumerate(sites, 1):
            models = {p.hardware_id: p.model for p in site.panels if p.hardware_id}
            lines += [
                "",
                f"== site {number}: {len(site.panels)} panel(s): "
                + ", ".join(p.model for p in site.panels),
            ]

            device = str(uuid.uuid4())
            directive = backend.parse_ably_token(
                grpc.ably_token(field_string(1, device)),
                fallback_channel=f"c:{user_id}:{device}",
            )
            channel = directive.channel or f"c:{user_id}:{device}"
            token = directive.token or cloud_ably.request_token(directive.token_request).token
            threading.Thread(
                target=_listen,
                args=(token, channel, f"site {number}", Recorder(redact), stop, errors),
                daemon=True,
            ).start()
            time.sleep(ATTACH_SECONDS)

            traits = WIDE_TRAITS if wide else backend.SUBSCRIBE_TRAITS
            try:
                snapshot = grpc.call(
                    "SubscribeAndGetTraits", subscribe_request(channel, site.hardware_ids, traits)
                )
                lines.append(f"subscribed to {len(traits)} trait ids")
            except cloud_grpc.GrpcError as exc:
                lines.append(f"wide subscribe refused ({exc.status}); using the app's 31")
                snapshot = grpc.call(
                    "SubscribeAndGetTraits",
                    subscribe_request(channel, site.hardware_ids, backend.SUBSCRIBE_TRAITS),
                )

            per_resource = snapshot_entries(snapshot)
            for resource, entries in per_resource.items():
                circuits = parse_trait_snapshot(
                    field_message(1, _resource_block(snapshot, resource))
                )
                label = models.get(resource, "site/gateway resource")
                lines += [
                    "",
                    f"=== resource {redact.text(resource)} ({label}): "
                    f"{len(entries)} entries, {len(circuits)} circuits",
                ]
                lines += trait_lines(entries, circuits, schema, redact)

            # The same traits, asked for without a subscription: may hold ones
            # the snapshot leaves out.
            try:
                latest = grpc.call("ListLatestTraitData", latest_request(site.hardware_ids, traits))
                lines += ["", "== ListLatestTraitData (raw) ==", *flatten(latest, redact)[:400]]
            except cloud_grpc.GrpcError as exc:
                lines += ["", f"== ListLatestTraitData refused: status {exc.status}"]

            try:
                feed = grpc.call(
                    "ListDispatches", field_string(1, site.site_id) + field_varint(2, 50)
                )
                lines += [
                    "",
                    "== ListDispatches (the app's feed) ==",
                    *dispatch_lines(feed, redact),
                ]
            except cloud_grpc.GrpcError as exc:
                lines += ["", f"== ListDispatches refused: status {exc.status}"]

    stop.set()
    lines += ["", *errors]
    return "\n".join(lines) + "\n"


def _resource_block(snapshot: bytes, resource: str) -> bytes:
    for block in parse(snapshot).values(1):
        ref = parse(block).get_msg_opt(1)
        if ref is not None and ref.get_str(1) == resource:
            return block
    return b""


def main() -> int:
    parser = argparse.ArgumentParser(description="List what SPAN's cloud reports.")
    parser.add_argument("--out", type=Path, default=Path("span-traits.txt"), help="report file")
    parser.add_argument(
        "--app-traits-only",
        action="store_true",
        help="ask only for the 31 traits the app subscribes to",
    )
    args = parser.parse_args()
    logging.disable(logging.INFO)

    email = input("SPAN email: ").strip()
    password = getpass.getpass("SPAN password: ")
    redact = Redactor()
    try:
        report = inventory(email, password, redact, wide=not args.app_traits_only)
        redact.assert_clean(report)
    except KeyboardInterrupt:
        print("\nStopped. No report written.")
        return 1
    except Exception as exc:  # noqa: BLE001 — the type only, whose text can quote ids
        print(f"Failed: {type(exc).__name__}. No report written.")
        return 1
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(report)
    print(f"Done: {args.out}. Read it before sharing it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
