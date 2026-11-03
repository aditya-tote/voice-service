from voice_harness.models import Brief
from voice_harness.personaplex import connection_url, prompt_for


def test_prompt_is_labeled_and_url_encoded():
    brief = Brief(
        leadId="lead",
        persona="p & q",
        goal="g",
        tone="t",
        facts=["f"],
        limits=["l"],
        voiceName="NATF2.pt",
    )
    assert "Persona:\np & q" in prompt_for(brief)
    assert "p+%26+q" in connection_url("ws://127.0.0.1:8998/api/chat", brief)
