"""
deck_engine/prompt_helpers.py — loads editable prompt templates for the
pipeline (PRD §4/§8: "prompts/bracket rules as editable config, not
hardcoded"). Templates live as plain text in deck_engine/prompts/ using
$placeholder syntax (string.Template, not str.format — the prompts contain
literal braces in JSON-shaped examples, which would break .format()).
"""
from __future__ import annotations

from string import Template

from . import config


def render(template_name: str, **kwargs) -> str:
    path = config.PROMPTS_DIR / template_name
    text = path.read_text()
    return Template(text).safe_substitute(**kwargs)


def bracket_rules_text() -> dict[str, str]:
    """Shared $bracket / $game_changers / $tutors / $combo_rule / $mld placeholders
    used across every pipeline prompt template — one place to keep the house
    rules (config.BRACKET_RULES) in sync with how they read in a prompt."""
    rules = config.BRACKET_RULES
    return {
        "bracket": config.BRACKET,
        "game_changers": "allowed" if rules["game_changers_allowed"] else "banned",
        "tutors": "allowed" if rules["tutors_allowed"] else "banned",
        "combo_rule": rules["two_card_infinite_combos"].replace("_", " "),
        "mld": "allowed" if rules["mass_land_destruction_allowed"] else "excluded",
    }
