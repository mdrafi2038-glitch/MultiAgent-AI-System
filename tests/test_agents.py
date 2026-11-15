from app.agents import AGENTS, choose_agents


def test_agent_registry_contains_core_roles():
    assert {"planner", "research", "writer", "coder", "data", "reviewer"}.issubset(AGENTS)


def test_coding_request_routes_to_coder():
    assert "coder" in choose_agents("Build a Python app and fix this bug")


def test_general_request_gets_default_specialists():
    assert choose_agents("Help me think through an idea") == ["research", "writer"]
