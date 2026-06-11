"""
UltimateCoders — Distributed AI Coding System

A multi-agent coding system with shared memory and multi-repo code retrieval.
"""

from ultimate_coders.agent import Orchestrator, Worker
from ultimate_coders.engine import Engine, create_engine
from ultimate_coders.memory import LongTermMemory, MemoryEntry, MemoryKey, ShortTermMemory

__version__ = "0.1.0"
__all__ = [
    "Engine",
    "create_engine",
    "Orchestrator",
    "Worker",
    "MemoryKey",
    "MemoryEntry",
    "ShortTermMemory",
    "LongTermMemory",
]
