"""Run with the enforce-type extra; validation and visibility remain explicit."""

from dataclasses import dataclass, field

from expose_api import App, MemoryStore
from expose_api.enforce_type import EnforceTypeSchema


@dataclass
class Note:
    id: str
    title: str
    password: str = field(default="", metadata={"expose_api": {"write_only": True}})


app = App(title="Validated notes")
notes = app.include(Note, store=MemoryStore(), public=True, schema=EnforceTypeSchema(Note))

if __name__ == "__main__":
    app.run()
