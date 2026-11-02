from _cli import claude


def translate(prompt, timeout, settings):
    return claude(prompt, timeout, "claude-haiku-5-5", "low")
