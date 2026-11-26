from studio import voice

HUMAN = (
    "Our server room flooded last March. We lost a day, not the data, because Dana had "
    "restored from the backup the Friday before. Test yours. It takes ten minutes and a "
    "spare laptop. Most offices skip it until the day it matters. Don't be that office."
)
ROBOT = (
    "In today's fast-paced world, it's important to note that your backup isn't just a "
    "safety net — it's a game-changer. Let's dive into how to unlock the power of a "
    "seamless, robust recovery journey!"
)


def codes(text: str, **kw) -> set[str]:
    return {f.code for f in voice.lint(text, banned=kw.pop("banned", []), **kw)}


def test_plain_human_text_has_no_findings():
    assert voice.lint(HUMAN, banned=[]) == []


def test_machine_habits_are_found():
    found = codes(ROBOT)
    assert {"ai_phrase", "not_just_but"} <= found
    matches = {f.match.lower() for f in voice.lint(ROBOT, banned=[])}
    assert any("game-changer" in m for m in matches)
    assert any("fast-paced" in m for m in matches)


def test_typographic_apostrophes_are_matched_too():
    assert "ai_phrase" in codes("In today" + chr(0x2019) + "s fast-paced world nothing waits.")


def test_owner_banned_phrases_block_approval():
    found = voice.lint("We offer synergy here.", banned=["synergy"])
    assert any(f.code == "banned_phrase" and f.severity == "error" for f in found)
    assert voice.blocks_approval(found)
    assert not voice.blocks_approval(voice.lint(ROBOT, banned=[]))


def test_banned_phrases_match_whole_words_only():
    assert "banned_phrase" not in codes("A cross-functional team.", banned=["fun"])


def test_too_many_em_dashes_and_exclamations_are_flagged():
    text = "One — two — three. Wow! Great! Amazing!"
    assert {"em_dash", "exclamations"} <= codes(text)


def test_emoji_follow_the_voice_setting():
    assert "emoji" in codes("Backups matter \U0001f680\U0001f525\U0001f4a1", emoji="light")
    assert "emoji" in codes("Backups matter \U0001f680", emoji="none")
    assert "emoji" not in codes("Backups matter \U0001f680", emoji="brand")


def test_monotone_sentence_length_is_flagged():
    flat = " ".join(["We fix your office network today."] * 7)
    assert "uniform_rhythm" in codes(flat)


def test_feedback_lists_what_to_remove():
    text = voice.feedback(voice.lint(ROBOT, banned=[]))
    assert "game-changer" in text and "Rewrite" in text


def test_empty_text_has_no_findings():
    assert voice.lint("   ", banned=["x"]) == []
