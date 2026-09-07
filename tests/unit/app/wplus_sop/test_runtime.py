# -*- coding: utf-8 -*-
import asyncio
import json
from types import SimpleNamespace

import pytest

from swe.app.wplus_sop.models import (
    MemoryCandidatesPayload,
    MemoryWriteBatchResultPayload,
    QuestionBatchPayload,
    SopResultPayload,
    StageProposalPayload,
    TrialExecutionCompletedPayload,
)
from swe.app.wplus_sop.runtime import (
    WPlusChatRunBusyError,
    WPlusSafeStreamTraceRegistry,
    build_wplus_command_text,
    get_wplus_safe_stream_trace_registry,
    start_wplus_chat_turn,
)
from swe.app.wplus_sop.runtime_context import get_current_wplus_runtime
from swe.app.answer_turn.models import TurnIdentity, TurnLease


class FakeTracker:
    def __init__(self, *, is_new=True):
        self.is_new = is_new
        self.call = None
        self.detached = None

    async def attach_or_start(
        self,
        identity,
        payload,
        stream_fn,
        *,
        before_start=None,
    ):
        if before_start is not None:
            before_start()
        if not self.is_new:
            return object(), False
        self.call = (
            identity,
            payload,
            stream_fn,
            get_current_wplus_runtime(),
        )
        return object(), True

    async def attach(self, _identity):
        return None

    async def detach_subscriber(self, identity, queue):
        self.detached = (identity, queue)

    async def stream(self, _identity, _queue):
        for item in ():
            yield item


class FakeCoordinator:
    def __init__(self, tracker):
        self.tracker = tracker

    async def start_or_attach(self, chat_id, payload, producer, **kwargs):
        identity = TurnIdentity(
            chat_id=chat_id,
            msgid=kwargs.get("msgid") or "msg-1",
            turn_id="turn-1",
        )
        queue, is_new = await self.tracker.attach_or_start(
            identity,
            payload,
            producer,
            before_start=kwargs.get("before_start"),
        )
        return TurnLease(identity, queue, is_new)


class FakeChannelManager:
    def __init__(self, channel):
        self.channel = channel

    async def get_channel(self, name):
        assert name == "console"
        return self.channel


def _sse(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def test_safe_stream_trace_collects_only_ordinary_assistant_text():
    registry = WPlusSafeStreamTraceRegistry(max_chars=1_000, max_lines=20)
    registry.start_run("sop-1", "run-1")

    frames = [
        {
            "object": "message",
            "id": "msg-answer",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": None,
        },
        {
            "object": "content",
            "msg_id": "msg-answer",
            "type": "text",
            "delta": True,
            "status": "in_progress",
            "text": "普通",
        },
        {
            "object": "content",
            "msg_id": "msg-answer",
            "type": "text",
            "delta": True,
            "status": "in_progress",
            "text": "回复",
        },
        {
            "object": "message",
            "id": "msg-reasoning",
            "type": "reasoning",
            "role": "assistant",
            "content": [{"type": "text", "text": "THOUGHT_SENTINEL"}],
        },
        {
            "object": "content",
            "msg_id": "msg-reasoning",
            "type": "text",
            "delta": True,
            "text": "REASONING_SENTINEL",
        },
        {
            "object": "message",
            "id": "msg-tool",
            "type": "function_call",
            "role": "assistant",
            "content": [{"type": "text", "text": "FUNCTION_SENTINEL"}],
        },
        {
            "object": "tool_output_frame",
            "source": "stdout",
            "text": "TOOL_SENTINEL",
        },
        {
            "object": "content",
            "msg_id": "unknown-message",
            "type": "text",
            "delta": True,
            "text": "UNKNOWN_SENTINEL",
        },
        {
            "object": "content",
            "msg_id": "msg-answer",
            "type": "data",
            "delta": True,
            "data": {"secret": "DATA_SENTINEL"},
        },
    ]

    for frame in frames:
        registry.ingest("sop-1", "run-1", _sse(frame))

    snapshot = registry.snapshot("sop-1", "run-1")
    assert snapshot is not None
    assert snapshot.summary_text == "普通回复"
    for sentinel in (
        "THOUGHT_SENTINEL",
        "REASONING_SENTINEL",
        "FUNCTION_SENTINEL",
        "TOOL_SENTINEL",
        "UNKNOWN_SENTINEL",
        "DATA_SENTINEL",
    ):
        assert sentinel not in snapshot.summary_text
    assert snapshot.truncated is False


def test_safe_stream_trace_projects_sanitized_tool_activity_in_order():
    registry = WPlusSafeStreamTraceRegistry(max_chars=1_000, max_lines=20)
    registry.start_run("sop-1", "run-1")

    frames = [
        {
            "object": "message",
            "id": "msg-answer",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [{"type": "text", "text": "正在核对客户范围。"}],
        },
        {
            "object": "message",
            "id": "msg-tool-start",
            "type": "function_call",
            "role": "assistant",
            "status": "in_progress",
            "content": [
                {
                    "type": "data",
                    "data": {
                        "call_id": "call-1",
                        "name": "execute_shell_command",
                        "arguments": {"command": "SECRET_COMMAND"},
                    },
                },
            ],
        },
        {
            "object": "message",
            "id": "msg-tool-finish",
            "type": "function_call_output",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "data",
                    "data": {
                        "tool_call_id": "call-1",
                        "name": "execute_shell_command",
                        "output": "SECRET_OUTPUT",
                    },
                },
            ],
        },
        {
            "object": "message",
            "id": "msg-final",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "text", "text": "已完成范围核对。"}],
        },
    ]

    for frame in frames:
        registry.ingest("sop-1", "run-1", _sse(frame))

    snapshot = registry.snapshot("sop-1", "run-1")
    assert snapshot is not None
    assert [entry.kind for entry in snapshot.entries] == [
        "assistant_text",
        "tool",
        "assistant_text",
    ]
    assert snapshot.entries[0].text == "正在核对客户范围。"
    assert snapshot.entries[1].tool_name == "execute_shell_command"
    assert snapshot.entries[1].status == "completed"
    assert snapshot.entries[2].text == "已完成范围核对。"
    rendered = json.dumps(
        [entry.to_dict() for entry in snapshot.entries],
        ensure_ascii=False,
    )
    assert "SECRET_COMMAND" not in rendered
    assert "SECRET_OUTPUT" not in rendered


def test_safe_stream_trace_final_content_replaces_incremental_body():
    registry = WPlusSafeStreamTraceRegistry(max_chars=1_000, max_lines=20)
    registry.start_run("sop-1", "run-1")
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "msg-answer",
                "type": "message",
                "role": "assistant",
                "content": None,
            },
        ),
    )
    for text in ("草稿", "内容"):
        registry.ingest(
            "sop-1",
            "run-1",
            _sse(
                {
                    "object": "content",
                    "msg_id": "msg-answer",
                    "type": "text",
                    "delta": True,
                    "text": text,
                },
            ),
        )
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "content",
                "msg_id": "msg-answer",
                "type": "text",
                "delta": False,
                "status": "completed",
                "text": "最终正文",
            },
        ),
    )

    snapshot = registry.snapshot("sop-1", "run-1")
    assert snapshot is not None
    assert snapshot.summary_text == "最终正文"
    assert snapshot.sequence == 3


def test_safe_stream_trace_completed_message_uses_nested_text_content():
    registry = WPlusSafeStreamTraceRegistry(max_chars=1_000, max_lines=20)
    registry.start_run("sop-1", "run-1")
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "msg-answer",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": None,
            },
        ),
    )
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "content",
                "msg_id": "msg-answer",
                "type": "text",
                "delta": True,
                "text": "旧草稿",
            },
        ),
    )
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "msg-answer",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "object": "content",
                        "type": "text",
                        "delta": False,
                        "text": "嵌套最终正文",
                    },
                    {"type": "data", "data": "NESTED_DATA_SENTINEL"},
                ],
            },
        ),
    )

    snapshot = registry.snapshot("sop-1", "run-1")
    assert snapshot is not None
    assert snapshot.summary_text == "嵌套最终正文"
    assert "NESTED_DATA_SENTINEL" not in snapshot.summary_text


def test_safe_stream_trace_has_fixed_capacity_and_evicts_old_runs():
    registry = WPlusSafeStreamTraceRegistry(
        max_chars=18,
        max_lines=2,
        max_active_runs=2,
    )
    registry.start_run("sop-1", "run-1")
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "msg-answer",
                "type": "message",
                "role": "assistant",
                "content": None,
            },
        ),
    )
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "content",
                "msg_id": "msg-answer",
                "type": "text",
                "delta": True,
                "text": "第一行很长\n第二行保留\n第三行保留",
            },
        ),
    )

    first = registry.snapshot("sop-1", "run-1")
    assert first is not None
    assert len(first.summary_text) <= 18
    assert len(first.summary_text.splitlines()) <= 2
    assert first.summary_text.endswith("第三行保留")
    assert first.truncated is True

    registry.start_run("sop-2", "run-2")
    registry.start_run("sop-3", "run-3")

    assert registry.snapshot("sop-1", "run-1") is None
    assert registry.snapshot("sop-2", "run-2") is not None
    assert registry.snapshot("sop-3", "run-3") is not None

    registry.start_run("sop-2", "run-4")
    assert registry.snapshot("sop-2", "run-2") is None
    assert registry.snapshot("sop-2", "run-4") is not None

    registry.finish_run("sop-2", "run-4")
    assert registry.snapshot("sop-2", "run-4") is None


def test_safe_stream_trace_sequence_only_increases_for_collected_text():
    registry = WPlusSafeStreamTraceRegistry(max_chars=1_000, max_lines=20)
    registry.start_run("sop-1", "run-1")
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": None,
            },
        ),
    )
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "content",
                "msg_id": "msg-1",
                "type": "text",
                "delta": True,
                "status": "in_progress",
                "text": "第一段",
            },
        ),
    )
    first = registry.snapshot("sop-1", "run-1")
    assert first is not None
    assert first.sequence == 1

    registry.ingest(
        "sop-1",
        "run-1",
        _sse({"object": "tool_output_frame", "text": "TOOL_SENTINEL"}),
    )
    second = registry.snapshot("sop-1", "run-1")
    assert second is not None
    assert second.sequence == 1
    assert second.summary_text == "第一段"


def test_command_text_contains_machine_readable_command():
    text = build_wplus_command_text(
        command="confirm_stage_queue",
        sop_session_id="sop-1",
        run_id="run-1",
        attempt_id="attempt-1",
        payload={"stages": [{"stage_id": "stage-1", "title": "筛选"}]},
    )

    body = json.loads(text.splitlines()[-1])
    assert body["command"] == "confirm_stage_queue"
    assert body["payload"]["stages"][0]["stage_id"] == "stage-1"
    assert "emit_wplus_sop_event" in text
    assert "CoPaw" not in text


def test_stage_proposal_command_requires_one_schema_valid_event():
    text = build_wplus_command_text(
        command="propose_stage_queue",
        sop_session_id="sop-1",
        run_id="run-1",
        attempt_id="attempt-1",
        payload={"original_request": "梳理客户筛选流程"},
    )

    assert "每个业务边界调用 emit_wplus_sop_event" in text
    assert "工具返回 ok=false" in text
    assert "kind='stage_proposal'" in text
    assert "不得把命令输入中的 payload 原样提交" in text
    assert "不得只输出 Markdown" in text
    assert "memory/common-wplus-knowledge.jsonl" in text
    assert "memory/cases/sop-cases.jsonl" in text
    example_marker = "stage_proposal payload 示例：\n"
    example = json.loads(
        text.split(example_marker, maxsplit=1)[1].splitlines()[0],
    )
    validated = StageProposalPayload.model_validate(example)

    assert len(validated.stages) == 2
    assert set(example) == {"stages"}
    assert all(
        set(stage)
        == {
            "stage_id",
            "name",
            "description",
            "status",
        }
        for stage in example["stages"]
    )


def test_retrying_stage_proposal_keeps_the_exact_event_contract():
    text = build_wplus_command_text(
        command="retry_current_turn",
        sop_session_id="sop-1",
        run_id="run-2",
        attempt_id="attempt-2",
        payload={
            "target_state": "GeneratingStageProposal",
            "retry_of_run_id": "run-1",
        },
    )

    assert "每个业务边界调用 emit_wplus_sop_event" in text
    assert "工具返回 ok=false" in text
    assert "kind='stage_proposal'" in text
    assert "stage_proposal payload 示例：" in text


@pytest.mark.parametrize(
    "target_state",
    ["GeneratingTrial", "ExecutingTrial"],
)
def test_trial_command_requires_same_background_turn_to_emit_terminal_event(
    target_state,
):
    text = build_wplus_command_text(
        command="submit_answers",
        sop_session_id="sop-1",
        run_id="run-1",
        attempt_id="attempt-1",
        payload={"current_stage_id": "stage-1"},
        target_state=target_state,
    )

    assert "同一个后台 Agent 回合" in text
    assert "不得在提交 trial_plan 后停止或等待另一个后台任务" in text
    assert "trial_execution_started" in text
    assert "直接执行 references 中已确认的 opencli 命令" in text
    assert "不得调用其他业务工具" in text
    assert "trial_execution_completed" in text
    assert "trial_execution_failed" in text
    assert 'run_id 必须严格等于命令中的 run_id="run-1"' in text
    assert 'attempt_id 必须严格等于命令中的 attempt_id="attempt-1"' in text
    assert "confirmed_facts" in text
    assert "unknowns" in text


@pytest.mark.parametrize(
    "target_state",
    ["GeneratingTrial", "ExecutingTrial"],
)
def test_trial_command_requires_decision_ready_detailed_results(target_state):
    text = build_wplus_command_text(
        command="submit_answers",
        sop_session_id="sop-1",
        run_id="run-1",
        attempt_id="attempt-1",
        payload={"current_stage_id": "stage-1"},
        target_state=target_state,
    )

    assert "不能只复述环节名称、用户输入或预跑目标" in text
    assert "执行范围、实际关键发现、可执行建议、证据与限制" in text
    assert "每个对象或分组一行" in text
    assert "对象分组、关键发现、建议动作、判断依据、影响数量" in text
    assert "total_count" in text
    assert "truncated" in text
    assert "不得为了满足详细度编造" in text
    assert "trial_execution_completed payload 示例" in text
    assert '"recommended_action"' in text
    assert '"evidence"' in text
    example = json.loads(
        text.split("真实脱敏结果）：\n", maxsplit=1)[1],
    )
    validated = TrialExecutionCompletedPayload.model_validate(example)
    assert validated.run_id == "run-1"
    assert validated.result_lists[0].total_count == 1


def test_stage_proposal_reads_personal_memory_only_with_anonymous_scope() -> (
    None
):
    text = build_wplus_command_text(
        command="propose_stage_queue",
        sop_session_id="sop-1",
        run_id="run-1",
        attempt_id="attempt-1",
        payload={
            "original_request": "梳理流程",
            "memory_user_scope": "anon_scope_123456",
        },
    )

    assert (
        "memory/users/anon_scope_123456/wplus-usage-preferences.jsonl" in text
    )
    assert "文件不存在表示当前没有对应记忆" in text


@pytest.mark.parametrize(
    ("command", "payload", "expected_sequence"),
    [
        (
            "confirm_stage",
            {},
            ["sop_result", "memory_candidates"],
        ),
        (
            "retry_current_turn",
            {
                "target_state": "FinalizingOutputs",
                "retry_of_run_id": "run-1",
                "final_result_persisted": True,
            },
            ["memory_candidates"],
        ),
    ],
)
def test_finalizing_outputs_has_an_explicit_terminal_event_sequence(
    command,
    payload,
    expected_sequence,
):
    text = build_wplus_command_text(
        command=command,
        sop_session_id="sop-1",
        run_id="run-2",
        attempt_id="attempt-2",
        payload=payload,
        target_state="FinalizingOutputs",
    )
    body = json.loads(text.splitlines()[1])

    assert body["expected_event_sequence"] == expected_sequence
    assert "同一个后台 Agent 回合" in text
    assert "不得提交 kind='retry_started'" in text
    assert "scripts/validate_sop.py" in text
    assert "scripts/render_md.py" in text
    assert "scripts/render_sop.py" in text
    assert "example_result.html" in text
    assert "逐个调用 copy_file_to_static" in text
    assert "不得使用 shell 复制到 static" in text
    assert "工具返回 ok=false" in text
    if payload.get("final_result_persisted"):
        assert "不得重复提交 sop_result" in text
    else:
        assert "先提交且只提交一个 sop_result" in text
        sop_example = json.loads(
            text.split("sop_result payload 示例：\n", maxsplit=1)[
                1
            ].splitlines()[0],
        )
        SopResultPayload.model_validate(sop_example)
    memory_example = json.loads(
        text.split("memory_candidates payload 示例：\n", maxsplit=1)[
            1
        ].splitlines()[0],
    )
    MemoryCandidatesPayload.model_validate(memory_example)
    assert "进入 OutputReview" in text
    assert "用户确认结果后才进入记忆处理" in text
    assert "不得伪造 approved" in text


@pytest.mark.parametrize(
    "is_final_stage",
    [False, True],
)
def test_cumulative_refresh_ends_before_the_next_agent_turn(
    is_final_stage: bool,
) -> None:
    text = build_wplus_command_text(
        command="confirm_stage",
        sop_session_id="sop-1",
        run_id="run-2",
        attempt_id="attempt-2",
        payload={
            "current_stage_id": "stage-2",
            "confirmed_snapshots": [
                {
                    "stage_id": "stage-1",
                    "report_no": 1,
                    "revision": 1,
                    "artifact_sha256": "0" * 64,
                },
            ],
            "is_final_stage": is_final_stage,
        },
        target_state="RefreshingCumulative",
    )
    body = json.loads(text.splitlines()[1])

    assert body["expected_event_sequence"] == ["cumulative_refreshed"]
    assert "同一个后台 Agent 回合" in text
    assert "cumulative_refreshed" in text
    assert "累计事件成功持久化后结束当前回合" in text
    assert "不得在本回合生成下一环节问题、执行预跑或生成最终结果" in text
    assert "sop_result" not in body["expected_event_sequence"]
    assert "memory_candidates" not in body["expected_event_sequence"]


def test_approved_memory_command_runs_bound_store_script_in_agent_turn() -> (
    None
):
    text = build_wplus_command_text(
        command="resolve_memory",
        sop_session_id="sop-1",
        run_id="run-memory",
        attempt_id="attempt-memory",
        target_state="WritingMemory",
        payload={
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "type": "common_wplus_knowledge",
                    "content": {"rule": "已确认规则"},
                    "evidence": "用户明确确认该规则。",
                    "target_scope": "common",
                    "target_file": "memory/common-wplus-knowledge.jsonl",
                    "script": "scripts/memory_store.py",
                    "approved": True,
                },
                {
                    "candidate_id": "candidate-2",
                    "type": "sop_case",
                    "content": {"pattern": "脱敏模式"},
                    "evidence": "用户确认该模式可复用。",
                    "target_scope": "cases",
                    "target_file": "memory/cases/sop-cases.jsonl",
                    "script": "scripts/memory_store.py",
                    "approved": True,
                },
            ],
        },
    )
    body = json.loads(text.splitlines()[1])

    assert body["expected_event_sequence"] == [
        "memory_write_batch_result",
    ]
    assert "execute_shell_command" in text
    assert "--approved" in text
    assert "同一个 Agent 回合" in text
    assert "candidate-1" in text
    assert "只提交一次 memory_write_batch_result" in text
    marker = "memory_write_batch_result 完整调用示例：\n"
    example = json.loads(text.split(marker, maxsplit=1)[1].splitlines()[0])
    assert example == {
        "kind": "memory_write_batch_result",
        "payload": {
            "results": [
                {
                    "candidate_id": "candidate-1",
                    "status": "succeeded",
                    "target_scope": "common",
                    "target_file": "memory/common-wplus-knowledge.jsonl",
                    "result": "appended",
                    "script": "scripts/memory_store.py",
                },
                {
                    "candidate_id": "candidate-2",
                    "status": "failed",
                    "error_code": "store_failed",
                    "summary": "脱敏后的失败原因",
                    "script": "scripts/memory_store.py",
                },
            ],
        },
        "event_key": "memory-write-batch-result-run-memory",
    }
    MemoryWriteBatchResultPayload.model_validate(example["payload"])


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        (
            "confirm_stage_queue",
            {
                "stages": [{"stage_id": "stage-1", "name": "确认范围"}],
                "current_stage_id": "stage-1",
            },
        ),
        (
            "confirm_stage",
            {
                "stage_id": "stage-1",
                "next_stage_id": "stage-2",
                "current_stage_id": "stage-2",
            },
        ),
        ("resume", {"current_stage_id": "stage-1"}),
        (
            "retry_current_turn",
            {
                "target_state": "GeneratingQuestions",
                "retry_of_run_id": "run-1",
                "current_stage_id": "stage-1",
            },
        ),
    ],
)
def test_generating_questions_requires_one_schema_valid_question_batch(
    command: str,
    payload: dict,
) -> None:
    text = build_wplus_command_text(
        command=command,
        sop_session_id="sop-1",
        run_id="run-2",
        attempt_id="attempt-2",
        payload=payload,
        target_state="GeneratingQuestions",
    )

    assert "[question_batch|trial_plan]" in text
    assert "不得提交 kind='stage_queue_confirmed'" in text
    assert "不得把命令输入中的 payload 原样提交" in text
    assert "每个业务边界调用 emit_wplus_sop_event" in text
    assert "工具返回 ok=false" in text
    assert (
        "question_batch.stage_id 必须严格等于命令 payload.current_stage_id="
        in text
    )
    assert json.dumps(payload["current_stage_id"], ensure_ascii=False) in text
    example_marker = "question_batch 最终 payload 示例：\n"
    example = json.loads(
        text.split(example_marker, maxsplit=1)[1].splitlines()[0],
    )
    validated = QuestionBatchPayload.model_validate(example)

    assert validated.stage_id == payload["current_stage_id"]
    assert 1 <= len(validated.questions) <= 3
    assert all(question.question_id for question in validated.questions)


@pytest.mark.asyncio
async def test_start_turn_forwards_target_state_into_agent_instruction():
    channel = SimpleNamespace(stream_one=object())
    tracker = FakeTracker()
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(channel),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )

    await start_wplus_chat_turn(
        workspace=workspace,
        chat=SimpleNamespace(id="chat-1", session_id="logical-1"),
        user_id="user-1",
        source_id="console",
        sop_session_id="sop-1",
        command="confirm_stage_queue",
        payload={"stages": [], "current_stage_id": "stage-1"},
        run_id="run-1",
        attempt_id="attempt-1",
        target_state="GeneratingQuestions",
    )

    native_payload = tracker.call[1]
    instruction = native_payload["content_parts"][0].text
    assert '"target_state": "GeneratingQuestions"' in instruction
    assert (
        '"expected_event_sequence": ["[question_batch|trial_plan]"'
        in instruction
    )
    assert native_payload["meta"]["wplus_sop_target_state"] == (
        "GeneratingQuestions"
    )


def test_retry_target_state_falls_back_to_payload_question_contract():
    text = build_wplus_command_text(
        command="retry_current_turn",
        sop_session_id="sop-1",
        run_id="run-2",
        attempt_id="attempt-2",
        payload={
            "target_state": "GeneratingQuestions",
            "retry_of_run_id": "run-1",
            "current_stage_id": "stage-2",
        },
    )

    assert "[question_batch|trial_plan]" in text
    assert 'payload.current_stage_id="stage-2"' in text
    assert "工具返回 ok=false" in text


@pytest.mark.asyncio
async def test_starts_one_turn_on_owning_chat_and_detaches_internal_queue():
    channel = SimpleNamespace(stream_one=object())
    tracker = FakeTracker()
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(channel),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )
    chat = SimpleNamespace(id="chat-1", session_id="logical-1")
    before_start_calls = 0

    def before_start():
        nonlocal before_start_calls
        before_start_calls += 1

    result = await start_wplus_chat_turn(
        workspace=workspace,
        chat=chat,
        user_id="user-1",
        source_id="console",
        sop_session_id="sop-1",
        command="submit_answers",
        payload={"answers": []},
        run_id="run-1",
        attempt_id="attempt-1",
        before_start=before_start,
    )

    identity, native_payload, stream_fn, trusted_runtime = tracker.call
    assert identity.chat_id == "chat-1"
    assert stream_fn is not channel.stream_one
    assert native_payload["meta"]["session_id"] == "logical-1"
    assert native_payload["meta"]["selected_skill_names"] == [
        "wplus-sop-miner",
    ]
    assert native_payload["meta"]["wplus_sop_session_id"] == "sop-1"
    assert trusted_runtime.run_id == "run-1"
    assert tracker.detached[0].chat_id == "chat-1"
    assert before_start_calls == 1
    assert result.run_id == "run-1"


@pytest.mark.asyncio
async def test_running_turn_freezes_process_local_safe_trace_on_completion():
    stream_ready = asyncio.Event()
    release_stream = asyncio.Event()
    completed = asyncio.Event()

    class StreamingTracker(FakeTracker):
        async def stream(self, _identity, _queue):
            yield _sse(
                {
                    "object": "message",
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": None,
                },
            )
            yield _sse(
                {
                    "object": "content",
                    "msg_id": "msg-1",
                    "type": "text",
                    "delta": True,
                    "status": "in_progress",
                    "text": "ACCOUNT_SENTINEL",
                },
            )
            stream_ready.set()
            await release_stream.wait()

    async def on_complete():
        completed.set()

    channel = SimpleNamespace(stream_one=object())
    tracker = StreamingTracker()
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(channel),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )

    await start_wplus_chat_turn(
        workspace=workspace,
        chat=SimpleNamespace(id="chat-1", session_id="logical-1"),
        user_id="user-1",
        source_id="console",
        sop_session_id="sop-1",
        command="submit_answers",
        payload={},
        run_id="run-1",
        attempt_id="attempt-1",
        on_complete=on_complete,
    )

    await asyncio.wait_for(stream_ready.wait(), timeout=1)
    snapshot = get_wplus_safe_stream_trace_registry(workspace).snapshot(
        "sop-1",
        "run-1",
    )
    assert snapshot is not None
    assert snapshot.summary_text == "ACCOUNT_SENTINEL"

    release_stream.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    completed_snapshot = get_wplus_safe_stream_trace_registry(
        workspace,
    ).snapshot(
        "sop-1",
        "run-1",
    )
    assert completed_snapshot is not None
    assert completed_snapshot.summary_text == "ACCOUNT_SENTINEL"
    registry = get_wplus_safe_stream_trace_registry(workspace)
    registry.ingest(
        "sop-1",
        "run-1",
        _sse(
            {
                "object": "message",
                "id": "late-message",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "late"}],
            },
        ),
    )
    assert (
        registry.snapshot("sop-1", "run-1").summary_text == "ACCOUNT_SENTINEL"
    )


@pytest.mark.asyncio
async def test_failed_stream_calls_on_complete_then_cleans_up():
    completed = asyncio.Event()

    class FailingTracker(FakeTracker):
        async def stream(self, _identity, _queue):
            yield _sse(
                {
                    "object": "tool_output_frame",
                    "source": "stderr",
                    "text": "TOOL_SECRET",
                },
            )
            raise RuntimeError("stream failed")

    async def on_complete():
        registry = get_wplus_safe_stream_trace_registry(workspace)
        snapshot = registry.snapshot("sop-1", "run-1")
        assert snapshot is not None
        assert snapshot.summary_text == ""
        assert "TOOL_SECRET" not in snapshot.summary_text
        completed.set()

    tracker = FailingTracker()
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(
            SimpleNamespace(stream_one=object()),
        ),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )

    await start_wplus_chat_turn(
        workspace=workspace,
        chat=SimpleNamespace(id="chat-1", session_id="logical-1"),
        user_id="user-1",
        source_id="console",
        sop_session_id="sop-1",
        command="submit_answers",
        payload={},
        run_id="run-1",
        attempt_id="attempt-1",
        on_complete=on_complete,
    )

    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    assert (
        get_wplus_safe_stream_trace_registry(workspace).snapshot(
            "sop-1",
            "run-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_cancelled_stream_calls_on_complete_then_cleans_up(monkeypatch):
    stream_ready = asyncio.Event()
    never_release = asyncio.Event()
    completed = asyncio.Event()
    created_tasks: list[asyncio.Task[None]] = []
    original_create_task = asyncio.create_task

    class BlockingTracker(FakeTracker):
        async def stream(self, _identity, _queue):
            yield _sse(
                {
                    "object": "content",
                    "type": "text",
                    "status": "in_progress",
                    "text": "CANCELLED_BODY_SENTINEL",
                },
            )
            stream_ready.set()
            await never_release.wait()

    def capture_task(coro):
        task = original_create_task(coro)
        created_tasks.append(task)
        return task

    async def on_complete():
        completed.set()

    monkeypatch.setattr(asyncio, "create_task", capture_task)
    tracker = BlockingTracker()
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(
            SimpleNamespace(stream_one=object()),
        ),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )

    await start_wplus_chat_turn(
        workspace=workspace,
        chat=SimpleNamespace(id="chat-1", session_id="logical-1"),
        user_id="user-1",
        source_id="console",
        sop_session_id="sop-1",
        command="submit_answers",
        payload={},
        run_id="run-1",
        attempt_id="attempt-1",
        on_complete=on_complete,
    )

    await asyncio.wait_for(stream_ready.wait(), timeout=1)
    assert len(created_tasks) == 1
    created_tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await created_tasks[0]

    assert completed.is_set()
    assert (
        get_wplus_safe_stream_trace_registry(workspace).snapshot(
            "sop-1",
            "run-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_refuses_to_attach_to_an_unrelated_existing_chat_run():
    channel = SimpleNamespace(stream_one=object())
    tracker = FakeTracker(is_new=False)
    workspace = SimpleNamespace(
        channel_manager=FakeChannelManager(channel),
        task_tracker=tracker,
        answer_turn_coordinator=FakeCoordinator(tracker),
    )

    with pytest.raises(WPlusChatRunBusyError):
        await start_wplus_chat_turn(
            workspace=workspace,
            chat=SimpleNamespace(id="chat-1", session_id="logical-1"),
            user_id="user-1",
            source_id="console",
            sop_session_id="sop-1",
            command="submit_answers",
            payload={},
            run_id="run-1",
            attempt_id="attempt-1",
        )

    assert tracker.detached[0].chat_id == "chat-1"
