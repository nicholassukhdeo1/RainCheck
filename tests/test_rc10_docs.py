from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_rc10_c29_agent_handoffs_agree_on_current_project_facts():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for document in (agents, claude):
        assert "schema version 2" in document.lower()
        assert "RC-06" in document and "merged" in document.lower()
        assert "seed input" in document.lower()
        assert "jobs read" in document.lower()
        assert "from the database" in document.lower()
