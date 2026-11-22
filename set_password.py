"""Sets, changes or removes Jarvis's password.

    python set_password.py                  set or change the password (asks you to type it)
    python set_password.py iron man sucks   set it directly
    python set_password.py --remove  turn the password off

Only a salted hash is saved (JARVIS_PASSWORD_HASH in .env), never the password itself.
Tip: use 2-4 ordinary words (for example "purple tiger seven") so speech recognition hears it reliably.
"""
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from automation import security  # noqa: E402

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def write_setting(value):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines()
                     if not re.match(r"\s*JARVIS_PASSWORD_HASH\s*=", line)]
    if value:
        lines.append(f"JARVIS_PASSWORD_HASH={value}")
    with open(ENV_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip("\n") + "\n")


def ask(prompt):
    """Hidden typing, falling back to normal typing where the terminal doesn't support it (e.g. VS Code)."""
    try:
        value = getpass.getpass(prompt)
    except Exception:
        value = ""
    if not value.strip():
        value = input(prompt.replace(":", " (it will show while you type):"))
    return value


def main():
    args = [a for a in sys.argv[1:] if a != "--remove"]
    if "--remove" in sys.argv:
        write_setting("")
        print("Password removed. Jarvis will no longer ask for one.")
        return
    if args:  # python set_password.py iron man sucks
        first = " ".join(args)
    else:
        print("Choose a password you can say clearly, e.g. 2-4 words.")
        first = ask("New password: ")
        if security.normalize(ask("Type it again: ")) != security.normalize(first):
            print("They didn't match. Nothing was changed.")
            return
    if len(security.normalize(first).replace(" ", "")) < 4:
        print("That's too short. Use at least 4 letters or numbers.")
        return
    write_setting(security.make_hash(first))
    print(f'Saved. Jarvis will accept it spoken or typed as: "{security.normalize(first)}"')
    print("Restart Jarvis (python main.py) for it to take effect.")


if __name__ == "__main__":
    main()
