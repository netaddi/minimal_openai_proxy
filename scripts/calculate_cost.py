#!/usr/bin/env python3
"""Calculate token cost from minimal_openai_proxy usage JSONL logs."""

from __future__ import annotations

import argparse
import collections
import datetime
import fnmatch
import json
import re
import sys
from typing import Iterable, Optional


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_cached_read_tokens",
    "input_cached_write_tokens",
    "output_reasoning_tokens",
    "output_cached_read_tokens",
    "input_audio_tokens",
    "output_audio_tokens",
)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def read_usage_records(paths: Iterable[str]) -> Iterable[dict]:
    for path in paths:
        with open(path, "r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSONL record: {exc}") from exc
                if isinstance(record, dict):
                    yield record


def record_model(record: dict, preferred_field: str = "auto") -> Optional[str]:
    if preferred_field != "auto":
        value = record.get(preferred_field)
        return value if isinstance(value, str) else None
    for field in ("response_model", "upstream_model", "request_model"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def model_rates(pricing: dict, model: str) -> Optional[dict]:
    models = pricing.get("models", {})
    if isinstance(models, dict) and isinstance(models.get(model), dict):
        return models[model]

    aliases = pricing.get("aliases", {})
    if isinstance(aliases, dict):
        aliased = aliases.get(model)
        if isinstance(aliased, str) and isinstance(models.get(aliased), dict):
            return models[aliased]

    for item in pricing.get("patterns", []):
        if not isinstance(item, dict):
            continue
        rates = item.get("rates")
        if not isinstance(rates, dict):
            continue
        pattern = item.get("pattern")
        glob = item.get("glob")
        if isinstance(pattern, str) and re.fullmatch(pattern, model):
            return rates
        if isinstance(glob, str) and fnmatch.fnmatch(model, glob):
            return rates
    return None


def usage_value(record: dict, field: str) -> int:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return 0
    value = usage.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def rate_value(rates: dict, field: str) -> Optional[float]:
    value = rates.get(field)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def calculate_record_cost(record: dict, pricing: dict, preferred_model_field: str = "auto") -> dict:
    model = record_model(record, preferred_model_field)
    if not model:
        return {"model": None, "matched": False, "cost": 0.0, "reason": "missing_model"}

    rates = model_rates(pricing, model)
    if rates is None:
        return {"model": model, "matched": False, "cost": 0.0, "reason": "missing_price"}

    unit_tokens_value = rates.get("unit_tokens") if rates.get("unit_tokens") is not None else pricing.get("unit_tokens", 1_000_000)
    unit_tokens = float(unit_tokens_value)
    if unit_tokens <= 0:
        raise ValueError("unit_tokens must be > 0")

    input_tokens = usage_value(record, "input_tokens")
    output_tokens = usage_value(record, "output_tokens")
    input_cached_read = usage_value(record, "input_cached_read_tokens")
    input_cached_write = usage_value(record, "input_cached_write_tokens")
    output_reasoning = usage_value(record, "output_reasoning_tokens")
    output_cached_read = usage_value(record, "output_cached_read_tokens")

    regular_input_tokens = input_tokens
    regular_output_tokens = output_tokens
    components = {}
    read_semantics = (
        rates.get("cached_read_semantics")
        or pricing.get("cached_read_semantics")
        or rates.get("cached_token_semantics")
        or pricing.get("cached_token_semantics")
        or "subset"
    )
    write_semantics = (
        rates.get("cached_write_semantics")
        or pricing.get("cached_write_semantics")
        or rates.get("cached_token_semantics")
        or pricing.get("cached_token_semantics")
        or "additive"
    )

    for token_field, rate_field, token_count, semantics in (
        ("input_cached_read_tokens", "input_cached_read", input_cached_read, read_semantics),
        ("input_cached_write_tokens", "input_cached_write", input_cached_write, write_semantics),
    ):
        rate = rate_value(rates, rate_field)
        if rate is not None:
            if semantics != "additive":
                regular_input_tokens = max(regular_input_tokens - token_count, 0)
            components[token_field] = {
                "tokens": token_count,
                "rate": rate,
                "cost": token_count * rate / unit_tokens,
            }

    for token_field, rate_field, token_count, semantics in (
        ("output_reasoning_tokens", "output_reasoning", output_reasoning, "subset"),
        ("output_cached_read_tokens", "output_cached_read", output_cached_read, read_semantics),
    ):
        rate = rate_value(rates, rate_field)
        if rate is not None:
            if semantics != "additive":
                regular_output_tokens = max(regular_output_tokens - token_count, 0)
            components[token_field] = {
                "tokens": token_count,
                "rate": rate,
                "cost": token_count * rate / unit_tokens,
            }

    for token_field, rate_field, token_count in (
        ("input_tokens", "input", regular_input_tokens),
        ("output_tokens", "output", regular_output_tokens),
        ("input_audio_tokens", "input_audio", usage_value(record, "input_audio_tokens")),
        ("output_audio_tokens", "output_audio", usage_value(record, "output_audio_tokens")),
    ):
        rate = rate_value(rates, rate_field)
        if rate is not None:
            components[token_field] = {
                "tokens": token_count,
                "rate": rate,
                "cost": token_count * rate / unit_tokens,
            }

    return {
        "model": model,
        "matched": True,
        "cost": sum(component["cost"] for component in components.values()),
        "components": components,
    }


def filter_records(records: Iterable[dict], start: Optional[str], end: Optional[str]) -> Iterable[dict]:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    has_window = start_dt is not None or end_dt is not None
    for record in records:
        record_dt = parse_timestamp(record.get("timestamp"))
        if has_window and record_dt is None:
            continue
        if start_dt and record_dt and record_dt < start_dt:
            continue
        if end_dt and record_dt and record_dt >= end_dt:
            continue
        yield record


def summarize(records: Iterable[dict], pricing: dict, preferred_model_field: str) -> dict:
    currency = pricing.get("currency", "USD")
    by_model = collections.defaultdict(lambda: {"requests": 0, "cost": 0.0, "usage": collections.Counter()})
    unmatched = collections.Counter()
    total = {"requests": 0, "cost": 0.0, "usage": collections.Counter()}

    for record in records:
        result = calculate_record_cost(record, pricing, preferred_model_field)
        model = result.get("model") or "unknown"
        bucket = by_model[model]
        bucket["requests"] += 1
        total["requests"] += 1

        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        for field in TOKEN_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                bucket["usage"][field] += value
                total["usage"][field] += value

        if result["matched"]:
            bucket["cost"] += result["cost"]
            total["cost"] += result["cost"]
        else:
            unmatched[result["reason"]] += 1

    return {
        "currency": currency,
        "unit_tokens": pricing.get("unit_tokens", 1_000_000),
        "total": {
            "requests": total["requests"],
            "cost": total["cost"],
            "usage": dict(total["usage"]),
        },
        "by_model": {
            model: {
                "requests": values["requests"],
                "cost": values["cost"],
                "usage": dict(values["usage"]),
            }
            for model, values in sorted(by_model.items())
        },
        "unmatched": dict(unmatched),
    }


def print_table(summary: dict) -> None:
    currency = summary["currency"]
    print(f"Currency: {currency}")
    print(
        "model requests input cached_read cache_write output reasoning total_tokens cost",
    )
    for model, values in summary["by_model"].items():
        usage = values["usage"]
        print(
            model,
            values["requests"],
            usage.get("input_tokens", 0),
            usage.get("input_cached_read_tokens", 0),
            usage.get("input_cached_write_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("output_reasoning_tokens", 0),
            usage.get("total_tokens", 0),
            f"{values['cost']:.8f}",
        )
    print(f"TOTAL {summary['total']['requests']} cost={summary['total']['cost']:.8f} {currency}")
    if summary["unmatched"]:
        print(f"Unmatched records: {summary['unmatched']}", file=sys.stderr)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate token cost from proxy usage JSONL logs.")
    parser.add_argument("usage_logs", nargs="+", help="One or more usage JSONL files.")
    parser.add_argument("--pricing", required=True, help="Pricing JSON file.")
    parser.add_argument("--model-field", default="auto", help="auto, response_model, upstream_model, or request_model.")
    parser.add_argument("--start", help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--end", help="Exclusive ISO timestamp upper bound.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    pricing = load_json(args.pricing)
    records = filter_records(read_usage_records(args.usage_logs), args.start, args.end)
    summary = summarize(records, pricing, args.model_field)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
