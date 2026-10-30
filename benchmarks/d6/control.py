"""Run the frozen schedule, rotating ledgers only at safe task boundaries."""

import fcntl
import json

from .budget import current, rotate
from .protocol import WORK, verify_protocol
from .run import run


def main():
    verify_protocol()
    WORK.mkdir(exist_ok=True, parents=True)
    with (WORK / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            database, _ = current()
            state = run(database)
            print(
                json.dumps(
                    {
                        "status": state["status"],
                        "completed": sum(
                            x["status"] == "finished" for x in state["tasks"].values()
                        ),
                        "B1_completed": sum(
                            x["status"] == "finished" for x in state["B1"].values()
                        ),
                    }
                ),
                flush=True,
            )
            if state["status"] == "finished":
                return 0
            if state["status"] != "awaiting_next_ledger":
                return 1
            rotate()


if __name__ == "__main__":
    raise SystemExit(main())
