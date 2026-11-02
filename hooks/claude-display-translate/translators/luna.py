from _cli import codex


def translate(prompt, timeout, settings):
    return codex(prompt, timeout, "gpt-6-luna", "low")
