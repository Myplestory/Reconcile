"""Discord Snowflake timestamp validation.

Decomposes 64-bit Snowflake IDs into:
- 42-bit millisecond timestamp (Discord epoch: 2015-01-01T00:00:00Z)
- 5-bit worker ID, 5-bit process ID, 12-bit increment

Cross-validates against API-reported timestamps (2s threshold).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..normalize.types import Message

DISCORD_EPOCH_MS = 1420070400000


def snowflake_decompose(snowflake_str: str) -> dict:
    """Decompose a Discord Snowflake into constituent fields."""
    sf = int(snowflake_str)
    ts_ms = (sf >> 22) + DISCORD_EPOCH_MS
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return {
        "snowflake": snowflake_str,
        "timestamp_ms": ts_ms,
        "timestamp_utc": dt.strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
        "worker_id": (sf & 0x3E0000) >> 17,
        "process_id": (sf & 0x1F000) >> 12,
        "increment": sf & 0xFFF,
    }


def validate(messages: list[Message]) -> dict:
    """Validate Snowflake timestamps for all messages.

    Returns dict with total, anomalies count, per-channel monotonicity.
    """
    by_channel: dict[str, list] = {}
    for msg in messages:
        ch = msg.channel or "unknown"
        by_channel.setdefault(ch, []).append(msg)

    all_anomalies = []
    channels_monotonic = 0
    total = 0

    for channel, msgs in by_channel.items():
        prev_ts = 0
        monotonic = True

        for msg in msgs:
            total += 1
            decomp = snowflake_decompose(msg.snowflake)
            ts_ms = decomp["timestamp_ms"]

            if ts_ms < prev_ts:
                monotonic = False
            prev_ts = ts_ms

            # Cross-check against API timestamp if available
            api_ts_str = msg.raw.get("timestamp", "") if msg.raw else ""
            if api_ts_str:
                try:
                    api_dt = datetime.fromisoformat(api_ts_str.replace("Z", "+00:00"))
                    sf_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    delta_s = round(abs((sf_dt - api_dt).total_seconds()), 3)
                    if delta_s > 2.0:
                        all_anomalies.append({
                            "message_id": msg.snowflake,
                            "channel": channel,
                            "snowflake_time": decomp["timestamp_utc"],
                            "delta_seconds": delta_s,
                        })
                except (ValueError, TypeError):
                    pass

        if monotonic:
            channels_monotonic += 1

    return {
        "total": total,
        "anomalies": len(all_anomalies),
        "anomaly_details": all_anomalies[:10],
        "channels_total": len(by_channel),
        "channels_monotonic": channels_monotonic,
    }
