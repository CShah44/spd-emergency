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

# App side: the request service calls the matching service for POST /match/{id}.
MATCHING_SERVICE_URL: str = os.environ.get("MATCHING_SERVICE_URL", "http://localhost:8001")
MATCH_TIMEOUT_S: float = _env_float("MATCH_TIMEOUT_S", 15.0)
# A queue message that fails this many times goes to the dead-letter queue (DESIGN.md section 7).
MAX_DELIVERY_ATTEMPTS: int = _env_int("MAX_DELIVERY_ATTEMPTS", 3)
