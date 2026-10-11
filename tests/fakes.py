"""Scripted stand-in for an AI provider (no network)."""
from eagleai.providers import Step


class FakeProvider:
    def __init__(self, script, brand="claude", model="fake-model"):
        self.script = list(script)      # list of (text, [(name, args)]) per step
        self.brand, self.model = brand, model
        self.seen_history = None
        self.seen_tools = None
        self.seen_images = None
        self.n = 0

    def start(self, history, user_text, images=None):
        self.seen_history = history
        self.seen_images = images
        return [{"role": "user", "text": user_text}]

    def step(self, system, tools, native, on_text):
        self.seen_tools = [t["name"] for t in tools]
        text, calls = self.script.pop(0) if self.script else ("done", [])
        self.n += 1
        if text:
            on_text(text)
        calls = [(f"c{self.n}_{i}", n, a) for i, (n, a) in enumerate(calls)]
        return Step(text, calls, {"input_tokens": 1000, "output_tokens": 100}, not calls)

    def add_results(self, native, results):
        native.append({"results": results})
