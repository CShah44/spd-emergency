"""Settings read from environment variables, with defaults for local runs.

Twelve-factor style: the same image runs in Compose and Kubernetes, and only
the environment (Compose `environment:` / k8s ConfigMap) changes.
"""

import os


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


COORDINATOR_URL: str = os.environ.get("COORDINATOR_URL", "http://localhost:8000")
STORAGE_TIMEOUT_S: float = _env_float("STORAGE_TIMEOUT_S", 5.0)

NUM_SHARDS: int = _env_int("NUM_SHARDS", 3)
HEARTBEAT_INTERVAL_S: float = _env_float("HEARTBEAT_INTERVAL_S", 3.0)
HEARTBEAT_TIMEOUT_S: float = _env_float("HEARTBEAT_TIMEOUT_S", 10.0)

MAX_MATCH_KM: float = _env_float("MAX_MATCH_KM", 50.0)
RABBITMQ_URL: str = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
