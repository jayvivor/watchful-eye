"""Watchful Eye — a rules engine + LLM harness for beta-testing the board game."""

from .config import GameConfig, load_config
from .engine import Game

__all__ = ["GameConfig", "load_config", "Game"]
