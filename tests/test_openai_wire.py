"""(needs `pip install httpx` for the mock transport - test only)
Drive ResponsesProvider through the real OpenAI SDK against a mocked HTTP server."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx, openai
import test_engine as te
from eagleai.lbr_builder import build_lbr
from eagleai.providers import ResponsesProvider
from eagleai.tools import Project, Settings
from eagleai.agent import Agent

reqs = []
def handler(request: httpx.Request):
    body = json.loads(request.content)
    reqs.append((str(request.url), body))
    if len(reqs) == 1:
        out = [{"type": "reasoning", "id": "rs_1", "summary": []},
               {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "add_passive", "status": "completed",
                "arguments": json.dumps({"kind": "R", "value": "1k", "package": "0603"})}]
    else:
        out = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Added R1 (1k).", "annotations": []}]}]
    return httpx.Response(200, json={"id": f"resp_{len(reqs)}", "object": "response", "created_at": 0, "status": "completed",
                                     "model": body["model"], "output": out, "parallel_tool_calls": True, "tool_choice": "auto",
                                     "tools": [], "usage": {"input_tokens": 1200, "output_tokens": 50, "total_tokens": 1250,
                                     "input_tokens_details": {"cached_tokens": 1000}, "output_tokens_details": {"reasoning_tokens": 0}}})

tmp = Path(tempfile.mkdtemp())
build_lbr(te.RES, tmp / "lbr")
st = Settings(lib_dirs=[str(tmp / "lbr")], converted_dir=str(tmp / "c"), memory_path=str(tmp / "m.json"), scripts_dir=str(tmp / "s"))
p = Project(tmp / "p", st); p.load_libraries()
prov = ResponsesProvider("sk-test", "grok-4.7", "xai")
prov.client = openai.OpenAI(api_key="x", base_url="https://api.x.ai/v1", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
out = Agent(p).run("add a 1k resistor", provider=prov, brand="xai")
print(out)
assert "R1" in p.design.parts and p.design.parts["R1"].value == "1k"
url, b2 = reqs[1]
print(url); assert url.endswith("/v1/responses")
kinds = [i.get("type", i.get("role")) for i in b2["input"]]
print(kinds); assert kinds[-3:] == ["reasoning", "function_call", "function_call_output"]
assert b2["tools"][0]["type"] == "function" and "parameters" in b2["tools"][0] and b2["instructions"].startswith("You are")
c = p.costs.calls[0]; print(c); assert c.cache_read_tokens == 1000 and c.input_tokens == 200 and c.usd > 0
print("ALL OK")
