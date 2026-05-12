from __future__ import annotations

from .models import Budget
from .tokenizer import estimate_tokens


DEFAULT_PROFILE = "balanced"

PROFILES = {
    "lean": {
        "default_total": 700,
        "reserve_ratio": 0.24,
        "runtime_ratio": 0.10,
        "memory_ratio": 0.42,
        "runtime_max": 110,
        "min_reserve": 90,
    },
    "balanced": {
        "default_total": 1200,
        "reserve_ratio": 0.20,
        "runtime_ratio": 0.12,
        "memory_ratio": 0.50,
        "runtime_max": 160,
        "min_reserve": 120,
    },
    "deep": {
        "default_total": 2400,
        "reserve_ratio": 0.18,
        "runtime_ratio": 0.12,
        "memory_ratio": 0.58,
        "runtime_max": 260,
        "min_reserve": 180,
    },
}


def profile_names() -> list[str]:
    return sorted(PROFILES)


def default_budget(profile: str = DEFAULT_PROFILE) -> int:
    return _profile(profile)["default_total"]


def allocate_budget(request: str, total: int | None = None, profile: str = DEFAULT_PROFILE) -> Budget:
    settings = _profile(profile)
    total = total or int(settings["default_total"])
    if total < 300:
        raise ValueError("Budget must be at least 300 tokens for the MVP runner.")

    request_tokens = min(max(estimate_tokens(request), 80), total // 4)
    reserve = max(int(settings["min_reserve"]), int(total * settings["reserve_ratio"]))
    runtime = min(int(settings["runtime_max"]), max(60, int(total * settings["runtime_ratio"])))
    remaining = total - request_tokens - reserve - runtime
    memory = max(60, int(remaining * settings["memory_ratio"]))
    skills = max(60, remaining - memory)

    return Budget(
        profile=profile,
        total=total,
        request=request_tokens,
        runtime=runtime,
        memory=memory,
        skills=skills,
        reserve=reserve,
    )


def _profile(profile: str) -> dict[str, float | int]:
    if profile not in PROFILES:
        raise ValueError(f"Unknown budget profile: {profile}. Expected one of: {', '.join(profile_names())}")
    return PROFILES[profile]
