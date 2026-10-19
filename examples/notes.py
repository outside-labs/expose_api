"""Run with `uv run python examples/notes.py`. Storage lasts for this process."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from expose_api import App, MemoryStore


@dataclass
class Note:
    title: str
    content: str = ""
    id: UUID = field(default_factory=uuid4)


app = App(title="Notes API")
notes = app.include(Note, store=MemoryStore(), public=True)

if __name__ == "__main__":
    app.run()
