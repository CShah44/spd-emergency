"""The load: emergency requests arriving at a steady rate.

Not run by hand: loadtest/run.py starts Locust with the right settings. Every request the
API accepts (201) has its id written to the file named by IDS_FILE, so verify.py can check
afterwards that each one was answered. A request the API refused or did not answer is
counted by Locust as a failure and is NOT in that file: the client knows it was not accepted.
"""

import os
import random
import sys
from pathlib import Path

from locust import HttpUser, constant_throughput, events, task

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadtest.common import TYPES, random_point  # noqa: E402

PER_USER_RATE = float(os.environ.get("PER_USER_RATE", "5"))  # requests per second, per simulated user
IDS_FILE = os.environ.get("IDS_FILE", "")
_ids = None


@events.test_start.add_listener
def open_ids_file(environment, **_):
    global _ids
    if IDS_FILE:
        _ids = open(IDS_FILE, "a", buffering=1)  # a line at a time, so a killed run still leaves its ids


@events.test_stop.add_listener
def close_ids_file(environment, **_):
    if _ids:
        _ids.close()


class Reporter(HttpUser):
    wait_time = constant_throughput(PER_USER_RATE)
    rng = random.Random()

    @task
    def report_an_emergency(self):
        body = {"type": self.rng.choice(TYPES), "location": random_point(self.rng)}
        with self.client.post("/requests", json=body, name="POST /requests", catch_response=True) as response:
            if response.status_code == 201:
                if _ids:
                    _ids.write(response.json()["id"] + "\n")
            else:
                response.failure(f"{response.status_code}")
