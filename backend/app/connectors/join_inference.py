"""Join inference shared by connectors: heuristic foreign keys from *_id column names."""
from __future__ import annotations


def infer_joins(
    table_metadata: dict[str, list[dict]],
    table_set: set[str],
    distinct_counts: dict[str, dict[str, int]] | None = None,
    row_counts: dict[str, int] | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Heuristic FK inference: columns named *_id matching another table; then, with distinct
    counts, lookup keys (see _lookup_joins)."""
    inferred: list[dict] = []
    provenance: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()

    for table_name, columns in table_metadata.items():
        for col in columns:
            col_name: str = col["name"]
            if not col_name.endswith("_id"):
                continue
            # e.g. customer_id → customers
            stem = col_name[: -len("_id")]
            candidates = [stem, stem + "s", stem + "es"]
            for candidate in candidates:
                if candidate in table_set and candidate != table_name:
                    sorted_pair = sorted([table_name, candidate])
                    pair = (sorted_pair[0], sorted_pair[1])
                    if pair in seen:
                        break
                    seen.add(pair)
                    join_key = f"{table_name}.{col_name}={candidate}.{col_name}"
                    # Check if the target table actually has this column
                    target_cols = {c["name"] for c in table_metadata.get(candidate, [])}
                    if col_name in target_cols:
                        inferred.append({
                            "from": table_name,
                            "to": candidate,
                            "on": f"{table_name}.{col_name} = {candidate}.{col_name}",
                            "type": "LEFT",
                            "relationship": "many_to_one",  # orders.customer_id -> customers
                        })
                        provenance[join_key] = "heuristic"
                    break

    if distinct_counts and row_counts:
        for join in _lookup_joins(table_metadata, distinct_counts, row_counts, seen):
            inferred.append(join)
            provenance[join["on"]] = join.pop("provenance")
    return inferred, provenance


def _lookup_joins(table_metadata, distinct_counts, row_counts, seen) -> list[dict]:
    """A column ending in another table's unique key: trips.pulocationid -> taxi_zone_lookup.locationid
    (an uploaded file with an attached lookup file; the benchmark's taxi upload had no join at all,
    and every borough or zone question failed). The key must be ID-like and unique in its table, and
    the referring table larger. With two referring columns (pickup and drop-off) the first in column
    order is taken, and the provenance says which were left: the compiler joins a pair one way only."""
    joins = []
    for lookup, columns in table_metadata.items():
        rows = row_counts.get(lookup, -1)
        for key in columns:
            name = key["name"].lower()
            if not (name == "id" or name.endswith("id") or name.endswith("key")) or len(name) < 4:
                continue
            if rows <= 0 or distinct_counts.get(lookup, {}).get(key["name"], -1) < 0.95 * rows:
                continue
            for table, other in table_metadata.items():
                if table == lookup or row_counts.get(table, -1) <= rows:
                    continue
                pair = tuple(sorted([table, lookup]))
                if pair in seen:
                    continue
                matches = [c["name"] for c in other if c["name"].lower() != name and c["name"].lower().endswith(name)]
                if not matches:
                    continue
                seen.add(pair)
                note = "heuristic (lookup key)" + (f"; also matched {', '.join(matches[1:])}" if len(matches) > 1 else "")
                joins.append({"from": table, "to": lookup, "on": f"{table}.{matches[0]} = {lookup}.{key['name']}",
                              "type": "LEFT", "relationship": "many_to_one", "provenance": note})
    return joins
