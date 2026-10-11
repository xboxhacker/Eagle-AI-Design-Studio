"""Claude web search wiring with a fake anthropic client: pause_turn continuation + search cost."""
import sys, types, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class B:
    def __init__(self, **k): self.__dict__.update(k)
    def model_dump(self, exclude_none=True): return {k: v for k, v in self.__dict__.items() if v is not None}


def U(i, o, s):
    return B(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0,
             server_tool_use=B(web_search_requests=s))


class Msgs:
    def __init__(self, script): self.script, self.seen = script, []
    def create(self, **kw):
        self.seen.append(kw); return self.script.pop(0)
    def stream(self, **kw):
        self.seen.append(json.loads(json.dumps(kw, default=str)))
        msg = self.script.pop(0)
        class S:
            text_stream = [b.text for b in msg.content if b.type == "text"]
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s): return msg
        return S()


def fake_anthropic(script):
    m = types.ModuleType("anthropic")
    msgs = Msgs(script)
    m.Anthropic = lambda api_key=None: B(messages=msgs)
    sys.modules["anthropic"] = m
    return msgs


def test_ask_claude_pause_turn():
    from eagleai import jlc_ai
    parts = {"parts": [{"refs": "C1", "lcsc": "C15849"}]}
    msgs = fake_anthropic([
        B(content=[B(type="server_tool_use", id="s1", name="web_search", input={"query": "x"})],
          stop_reason="pause_turn", usage=U(1000, 50, 3)),
        B(content=[B(type="text", text="Here:\n" + json.dumps(parts))], stop_reason="end_turn", usage=U(2000, 200, 2)),
    ])
    got, usage, searches = jlc_ai.ask_claude([{"refs": "C1", "value": "1uF"}], "k", "claude-x", 3, 3.0)
    assert got == parts["parts"] and searches == 5 and usage["input_tokens"] == 3000
    assert msgs.seen[1]["messages"][-1]["role"] == "assistant"          # continued as-is
    assert msgs.seen[0]["tools"][0]["type"].startswith("web_search")


def test_chat_step_has_web_search_and_cost():
    from eagleai.providers import ClaudeProvider
    msgs = fake_anthropic([
        B(content=[B(type="text", text="searching")], stop_reason="pause_turn", usage=U(10, 5, 2)),
        B(content=[B(type="tool_use", id="t1", name="jlc_lookup", input={"lcsc": "C1"})], stop_reason="tool_use",
          usage=U(10, 5, 1)),
    ])
    prov = ClaudeProvider("k", "claude-x")
    native = prov.start([], "find parts")
    st = prov.step("sys", [{"name": "jlc_lookup", "description": "d", "input_schema": {"type": "object"}}], native,
                   lambda s: None)
    assert st.calls == [("t1", "jlc_lookup", {"lcsc": "C1"})] and not st.done
    assert abs(st.usage["extra_usd"] - 0.03) < 1e-9 and st.usage["input_tokens"] == 20
    assert msgs.seen[0]["tools"][0]["name"] == "web_search" and "allowed_domains" not in msgs.seen[0]["tools"][0]


if __name__ == "__main__":
    test_ask_claude_pause_turn(); print("ok pause_turn")
    test_chat_step_has_web_search_and_cost(); print("ok chat step")
