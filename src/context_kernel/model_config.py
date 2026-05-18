from __future__ import annotations

import argparse

from .providers import env_value


DEFAULT_PRIMARY_MODEL = "gpt-5.5"
DEFAULT_AUXILIARY_MODEL = "gpt-5.3-codex"


def primary_model(args: argparse.Namespace) -> str:
    return args.model or env_value("AKERNEL_OPENAI_MODEL") or DEFAULT_PRIMARY_MODEL


def auxiliary_model(args: argparse.Namespace) -> str:
    return getattr(args, "aux_model", None) or env_value("AKERNEL_OPENAI_AUX_MODEL") or DEFAULT_AUXILIARY_MODEL
