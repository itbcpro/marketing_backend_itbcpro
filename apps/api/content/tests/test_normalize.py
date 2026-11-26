from content.normalize import content_hash, normalize_html

HTML = """
<div><h1>Main title</h1><script>var x = 1;</script><style>p {color: red}</style>
<p>First paragraph with <a href="/inside">an inside link</a> and
<a href="https://www.example.com/also">same host</a>.</p>
<h2>Second   level</h2><p>Text <a href="https://other.org/x">out</a><a href="#top">skip</a>
<a href="mailto:a@b.c">mail</a></p>
<img src="1.png" alt="described"><img src="2.png"><img src="3.png" alt="  ">
</div>
"""


def test_normalize_extracts_text_outline_links_and_images():
    facts = normalize_html(HTML, "https://example.com")
    assert "var x" not in facts["text"]
    assert "color: red" not in facts["text"]
    assert facts["outline"] == [
        {"level": 1, "text": "Main title"},
        {"level": 2, "text": "Second level"},
    ]
    assert facts["stats"]["internal_links"] == 2
    assert facts["stats"]["external_links"] == 1
    assert facts["stats"]["images"] == 3
    assert facts["stats"]["images_missing_alt"] == 2
    assert facts["stats"]["word_count"] == len(facts["text"].split())


def test_normalize_empty_html():
    facts = normalize_html("", "https://example.com")
    assert facts["text"] == ""
    assert facts["stats"]["word_count"] == 0


def test_content_hash_changes_with_text_or_title():
    base = content_hash("A", "text")
    assert base == content_hash("A", "text")
    assert base != content_hash("B", "text")
    assert base != content_hash("A", "other")
