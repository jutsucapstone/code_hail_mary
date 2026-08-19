"""Write the OpenAPI document to stdout, without starting a server.

The schema is derived from the application object itself, so generating the TypeScript
client needs neither a running API nor a database. That matters for CI: the staleness
check has to be a pure function of the source, or it becomes a flaky test about whether
a server happened to be up.

`JUTSU_ENV` is forced away from prod because production deliberately serves no schema —
`create_app` returns an app with `openapi_url=None` there — but the document still has to
be generatable at build time.
"""

from __future__ import annotations

import json
import os
import sys

os.environ["JUTSU_ENV"] = "build"

from jutsu_api.main import create_app

if __name__ == "__main__":
    json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
