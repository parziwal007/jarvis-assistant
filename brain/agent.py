"""Turns a spoken or typed request into a plan of laptop actions and runs it.

One language-model call returns JSON like:
    {"actions": [{"tool": "open_app", "args": {"name": "spotify"}}], "reply": "..."}
The actions run in order through automation.tools.execute(). Questions and chat
come back with no actions, and "reply" is the answer.
"""
import datetime
import json
import logging
import re

from automation import app, tools
from brain import llm

MAX_ACTIONS = 6
logger = logging.getLogger(__name__)


def _planner_prompt(spoken=False):
    name = tools.user_name()
    example = json.dumps({
        "actions": [
            {"tool": "write_in_notepad", "args": {"write_about": "a 300-word essay about the sun", "title": "Sun essay"}},
        ],
        "reply": "Writing your essay on the sun in Notepad.",
    })
    now = datetime.datetime.now()
    return (
        f"You are Jarvis, {name}'s voice-controlled personal assistant running on their Windows laptop. "
        f"Current local date and time: {now:%A %d %B %Y, %I:%M %p}. "
        "You can operate the laptop with these tools:\n"
        f"{tools.describe_tools()}\n\n"
        "Respond with ONE JSON object and nothing else, in this shape:\n"
        f"{example}\n\n"
        "Rules:\n"
        "- If the user wants information or conversation only, return \"actions\": [] and put the answer in "
        "\"reply\": concise, natural spoken English, no markdown, no lists of symbols.\n"
        f"- If the user wants something done on the laptop, list the tool calls in order (at most {MAX_ACTIONS}). "
        "Then \"reply\" is one short line about what you're doing. The app reports the real results, so never "
        "claim something already happened.\n"
        "- For essays, letters, email bodies or any long writing, do NOT write the text yourself. Pass a clear "
        "instruction in \"write_about\" (or \"about\" for email) and the tool will write it.\n"
        "- \"Open Notepad and write X\" means write_in_notepad. \"Type X\" means type_text into the current window. "
        "\"Lower the voice/sound\" means set_volume. \"Play X\" or \"play X's latest video\" means youtube_play "
        "(it opens and starts the video). youtube_search only lists results. media_control only pauses, resumes "
        "or skips something already playing.\n"
        "- For email, pass the recipient's email if the user said it, otherwise their name exactly as said. "
        "Never invent an email address.\n"
        "- \"Message/WhatsApp/text X ...\" or \"write hi to X\" means whatsapp_message (one call per person). Put "
        "dictated words in \"text\" (\"write hi to Adi\" -> text \"hi\"; \"write high\" is a mishearing of "
        "\"write hi\"); use \"about\" only when Jarvis should "
        "compose it. If the user gave no content, ask what to say. Use whatsapp_send only when the user says "
        "\"send it\" (or similar) after a message is ready.\n"
        "- Private, incognito or InPrivate browsing means private_browse.\n"
        "- Spotify requests (\"play my liked songs\", \"play X on Spotify\") mean spotify_play, not youtube_play or "
        "open_app. Only use youtube_play when the user says YouTube or doesn't mention Spotify.\n"
        "- \"Read what you wrote\", \"read the essay/note in Notepad\" mean read_last_written. If the user asks to "
        "write something and read it out, set read_aloud true on write_in_notepad.\n"
        "- \"Open X folder\" or \"open X\" where X isn't an app means open_folder with X exactly as said "
        "(\"open solo leveling folder\" -> name \"solo leveling\"). Never replace it with \"project\".\n"
        "- \"Create/make a folder\" means create_folder. If the user gave the name in an earlier message "
        "(\"name it capstone\"), use it; never call open_folder for a folder that is being created.\n"
        "- \"Open the X folder in/through/with VS Code (or Visual Studio)\" means open_folder with app \"vs code\"; "
        "never just open VS Code on its own. \"Open that folder in VS Code\" uses name \"that folder\".\n"
        "- \"Look at / read / check / explain / review the code\", \"find bugs\", \"what does this project or "
        "file do\" mean review_code (pass the user's question; folder only if they named one, else it uses the "
        "folder just opened).\n"
        "- Files and folders: \"delete X\" means delete_item (it asks for confirmation; never call confirm_delete "
        "in the same plan). \"Yes, delete it\" right after Jarvis asked means confirm_delete. \"Save/move/put X in "
        "the Y folder\" means save_to_folder (copy true if they said copy). \"Write an essay ... and save it in "
        "the Y folder\" means write_in_notepad with folder Y. \"That folder\" means the folder Jarvis just opened "
        "or created.\n"
        "- Recording: \"record a video of me\", \"record from my camera\", or \"start recording\" right after the "
        "Camera app was opened mean camera_record. \"Record my screen\" means screen_record. \"Stop recording\" "
        "means stop_recording. Never say you can't record.\n"
        "- \"Close/terminate/kill X\" means close_app; set force true only for terminate, kill or force close.\n"
        "- Volume: \"set the volume to 40\" means level 40; \"increase/raise the volume by 20%\" means change "
        "\"up\" with amount 20; \"decrease/lower it by 20\" means change \"down\" with amount 20.\n"
        "- \"Most liked/most viewed/most popular video\" means youtube_play with \"most viewed\" in the query.\n"
        "- Websites like YouTube are browser tabs, not apps: \"close the video/tab\" means close_tab, never close_app.\n"
        "- \"Save X's number/email ...\" means add_contact.\n"
        "- \"Send it\" (after Jarvis prepared a message, draft or reply) means send_pending. If the user names a "
        "person (\"send the mail to Priya\"), pass that name as \"to\" so Jarvis can check it's the right "
        "message. If they want to send something new to someone, use whatsapp_message or draft_email instead.\n"
        "- Speech recognition often hears \"mails\" as \"meals\", \"males\" or \"nails\": \"summarize my meals\" "
        "means summarize my mails (check_email).\n"
        "- \"Check my mail\", \"who mailed me\", \"any important emails\" mean check_email. \"Read the mail from "
        "Adi\" means check_email with from_name \"Adi\" and read_full true.\n"
        "- \"Reply to Adi's email\" means reply_to_email. Only pass instructions the user actually gave; never "
        "make up what the reply should say. If the user "
        "asks to change a reply Jarvis just drafted, call reply_to_email again with the new instructions.\n"
        "- \"I'm going out\", \"I'm busy\", \"I'm not available\" mean set_availability away; \"I'm back\" "
        "means available.\n"
        "- \"What's my schedule\", \"am I free at 4\", \"what do I have tomorrow\" mean get_schedule. Never "
        "answer schedule questions from memory.\n"
        "- Meetings or events at a time mean schedule_event with start as YYYY-MM-DDTHH:MM. A bare hour like "
        "\"at 3\" means 3 PM; if that time has already passed today, use tomorrow.\n"
        "- The text comes from speech recognition and often mishears names. Before using an artist, song, app or "
        "place name, correct obvious mishearings to the real name (\"colonel ojla songs\" -> \"Karan Aujla songs\", "
        "\"please i'm X from youtube\" -> play X on YouTube). Never change contact names this way.\n"
        "- Use only the tools and argument names listed, and always fill in every required argument "
        "(for example web_search needs \"query\"). Never do anything the user did not ask for.\n"
        "- If the request is unclear or missing something essential, ask one short question in \"reply\" "
        "with no actions.\n"
        f"- Address the user as {name} occasionally, not in every reply."
        + ("\n- This request was heard by the microphone, which also picks up other people talking. If it is "
           "clearly not meant for Jarvis (a fragment, filler, or someone talking to another person, e.g. "
           "\"uh-huh what's all that\"), return {\"actions\": [], \"reply\": \"\"} so Jarvis stays quiet."
           if spoken else "")
    )


ARG_KEYS = ("args", "arguments", "parameters", "params", "input", "inputs")


def _normalize_action(item):
    """Accepts the different ways models write a tool call:
    {"tool": "open_app", "args": {...}}, {"tool": ..., "arguments": "{...}"},
    {"name": "open_app", "parameters": {...}}, or {"tool": "open_app", "name": "youtube"}."""
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("function"), dict):  # OpenAI style {"function": {"name", "arguments"}}
        item = {**item["function"], **{k: v for k, v in item.items() if k != "function"}}
    tool = item.get("tool") or item.get("action") or item.get("function")
    tool_key = "tool" if item.get("tool") else ("action" if item.get("action") else "function")
    if not isinstance(tool, str) and isinstance(item.get("name"), str) and item["name"] in tools.TOOLS:
        tool, tool_key = item["name"], "name"
    if not isinstance(tool, str) or not tool.strip():
        return None
    args = None
    for key in ARG_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if isinstance(value, dict):
            args = value
            break
    if args is None:  # arguments written next to the tool name
        args = {k: v for k, v in item.items() if k not in (tool_key, *ARG_KEYS, "id", "type")}
    return {"tool": tool.strip(), "args": args}


def parse_plan(text):
    """Extracts {"actions": [...], "reply": str} from model output, or returns None."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    actions = [action for action in map(_normalize_action, data.get("actions") or []) if action]
    reply = data.get("reply")
    return {"actions": actions[:MAX_ACTIONS], "reply": reply.strip() if isinstance(reply, str) else ""}


SEARCH_TOOLS = {"web_search", "youtube_search", "youtube_play"}
SEARCH_FILLER = re.compile(
    r"^(?:jarvis\W*)?(?:(?:can|could|would) you |please |i (?:just )?want to know |tell me |"
    r"search (?:for )?|google |look up |find (?:out )?)+",
    re.IGNORECASE,
)


def _fill_missing_query(action, user_text):
    """Models sometimes pick web_search but forget the query: use the user's words."""
    if action["tool"] in SEARCH_TOOLS and not str(action["args"].get("query") or "").strip():
        query = SEARCH_FILLER.sub("", user_text.strip()).strip(" ?.!")
        action["args"] = {**action["args"], "query": query or user_text}


def handle(user_text, source="typed"):
    """Plans and runs a request. Returns a string to show and speak ("" = stay quiet), or a ConfirmationRequest."""
    spoken = source == "voice"
    messages = [
        *llm.CONVERSATION_HISTORY[-llm.MAX_HISTORY_MESSAGES:],
        {"role": "user", "content": user_text},
    ]
    content, ok = llm.complete(messages, _planner_prompt(spoken), max_tokens=llm.CHAT_MAX_TOKENS, json_mode=True)
    if not ok:
        return content

    plan = parse_plan(content)
    if plan is None:
        # The model answered in plain text instead of JSON: treat it as a normal reply.
        reply = content.strip()
        llm.remember_exchange(user_text, reply)
        return reply

    if not plan["actions"]:
        if not plan["reply"] and spoken:
            print("[Agent] Not addressed to Jarvis; staying quiet.")
            return ""
        reply = plan["reply"] or "Sorry, I didn't catch that. Could you say it again?"
        llm.remember_exchange(user_text, reply)
        return reply

    results = []
    for action in plan["actions"]:
        _fill_missing_query(action, user_text)
        print(f"[Agent] {action['tool']} {action['args']}")
        try:
            outcome = tools.execute(action["tool"], action["args"])
        except tools.ToolError as exc:
            results.append(f"I couldn't finish that: {exc}")
            break
        if isinstance(outcome, app.ConfirmationRequest):
            done = " ".join(results)
            prompt = f"{done} {outcome.prompt}".strip()
            llm.remember_exchange(user_text, prompt)
            return app.ConfirmationRequest(outcome.action, outcome.payload, prompt)
        if outcome:
            results.append(str(outcome))

    summary = " ".join(results) or plan["reply"] or "Done."
    llm.remember_exchange(user_text, summary)
    return summary
