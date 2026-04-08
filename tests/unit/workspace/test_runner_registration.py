# -*- coding: utf-8 -*-
"""Regression tests for workspace runner registration."""


def test_runner_lazy_export_resolves_class():
    """AgentRunner package export should resolve to the concrete class."""
    from swe.app.runner import AgentRunner

    assert AgentRunner is not None
    assert AgentRunner.__name__ == "AgentRunner"


def test_workspace_registers_concrete_runner_service(tmp_path):
    """Workspace should register a concrete runner service class."""
    from swe.app.workspace import Workspace

    workspace = Workspace(
        agent_id="test123",
        workspace_dir=str(tmp_path / "test_agent"),
    )

    descriptor = workspace._service_manager.descriptors["runner"]  # pylint: disable=protected-access

    assert descriptor.service_class is not None
