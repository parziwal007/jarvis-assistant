"""Run from the Jarvis_Assistant folder:  python jarvis_check.py
Checks each part of Jarvis step by step and prints what is wrong."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


def step(title):
    print("\n" + "=" * 60 + f"\n{title}\n" + "=" * 60)


step("1. Are the patched files in place?")
for path in ["automation/tools.py", "brain/agent.py", "brain/llm.py", "ears/listener.py", "main.py", ".env"]:
    print(f"  {'OK     ' if os.path.isfile(path) else 'MISSING'} {path}")
if not os.path.isfile("brain/agent.py"):
    print("\n  >> The patch was not copied into this folder. Copy the CONTENTS of the\n"
          "     Jarvis_Assistant_patch folder (not the folder itself) into Jarvis_Assistant.")
    sys.exit(1)

step("2. Is .env loaded?")
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(HERE, ".env"))
    print("  OK      python-dotenv installed")
except ImportError:
    print("  MISSING python-dotenv  ->  run: pip install python-dotenv")
for key in ["JARVIS_GEMINI_API_KEY", "OPENROUTER_API_KEY", "XAI_API_KEY", "JARVIS_CLOUD_PROVIDER"]:
    value = os.getenv(key, "").strip()
    shown = value if key == "JARVIS_CLOUD_PROVIDER" else ("set" if value else "empty")
    print(f"  {key:24} {shown or 'empty'}")

step("3. Can Jarvis reach a language model?")
from brain import llm

print("  Cloud providers with keys:", llm.get_cloud_provider_candidates() or "none")
for provider in llm.get_cloud_provider_candidates():
    print(f"  {provider:10} reachable: {llm.is_internet_available(provider)}")
print("  Ollama running:", llm.is_ollama_available())
reply, ok = llm.complete([{"role": "user", "content": "Reply with exactly: pong"}], "You are a test.", max_tokens=200)
print(f"  Test reply ({'OK' if ok else 'FAILED'}): {reply[:300]!r}")
if not ok:
    print("\n  >> No model answered. Fix the key/internet, or start Ollama and run:\n"
          "     ollama pull qwen2.5:7b-instruct")
    sys.exit(1)

step("4. What does the agent plan for a task?")
from brain import agent

request = "open notepad and write a short essay about the sun"
raw, ok = llm.complete([{"role": "user", "content": request}], agent._planner_prompt(), max_tokens=llm.CHAT_MAX_TOKENS, json_mode=True)
print("  Raw model output:\n   ", raw[:800])
plan = agent.parse_plan(raw)
print("  Parsed plan:", plan)
if not plan or not plan["actions"]:
    print("\n  >> The model did not return a usable plan. Send this output to Claude.")
    sys.exit(1)

step("5. Does the quick-command router hand it to the agent?")
from automation import app

print("  trigger_action returned:", repr(app.trigger_action(request)), "(should be None)")

step("6. Run the whole thing (Notepad should open with an essay)")
try:
    import main

    print("  Jarvis says:", main.handle_command(request)[0])
except Exception:
    traceback.print_exc()

print("\nDone. Copy everything above and send it to Claude if something failed.")
