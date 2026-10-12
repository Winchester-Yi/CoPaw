import pytest
from pydantic import ValidationError

from swe.jev.config import JevConfig


def test_disabled_config_needs_no_service():
    config = JevConfig()
    assert not config.skill_selection.enabled
    assert not config.search_filtering.enabled
    assert not config.subagent_model_selection.enabled


@pytest.mark.parametrize(
    "search",
    [
        {"enabled": True},
        {"enabled": True, "top_k": 0, "threshold": 0.5},
        {"enabled": True, "top_k": True, "threshold": 0.5},
        {"enabled": True, "top_k": 2, "threshold": float("nan")},
        {"enabled": True, "top_k": 2, "threshold": 1.1},
    ],
)
def test_rejects_invalid_search_config(search):
    with pytest.raises(ValidationError):
        JevConfig(
            endpoint="https://jev.test/decide",
            timeout_seconds=2,
            search_filtering=search,
        )


def test_enabled_service_requires_endpoint_and_timeout():
    with pytest.raises(ValidationError):
        JevConfig(subagent_model_selection={"enabled": True})


def test_explicit_values_and_snapshot_are_preserved():
    config = JevConfig(
        endpoint="https://jev.test/decide",
        timeout_seconds=2,
        search_filtering={"enabled": True, "top_k": 7, "threshold": 0.62},
    )
    snapshot = config.model_copy(deep=True)
    config.search_filtering.top_k = 9
    assert snapshot.search_filtering.top_k == 7
    assert snapshot.search_filtering.threshold == 0.62


def test_agent_file_config_round_trips_jev():
    from swe.config.config import AgentProfileConfig

    config = AgentProfileConfig.model_validate(
        {
            "id": "test",
            "name": "Test",
            "jev": {
                "endpoint": "https://jev.test/decide",
                "timeout_seconds": 2,
                "search_filtering": {
                    "enabled": True,
                    "top_k": 3,
                    "threshold": 0.7,
                },
            },
        }
    )
    saved = config.model_dump(mode="json")
    assert saved["jev"]["search_filtering"]["top_k"] == 3
    assert AgentProfileConfig.model_validate(
        saved
    ).jev.search_filtering.enabled
