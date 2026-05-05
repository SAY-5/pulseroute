"""Run a suite against one or more candidate providers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from pulseroute_router.provider import ChatProvider
from pulseroute_router.providers.fake import FakeProvider
from pulseroute_shared.types import ChatCompletionRequest, ChatMessage

from pulseroute_eval.scoring import score
from pulseroute_eval.suites import GOLDEN_SUITE, EvalTask


@dataclass
class TaskResult:
    task_id: str
    category: str
    score: float
    latency_ms: int
    output: str


@dataclass
class SuiteResult:
    model: str
    tasks: list[TaskResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if not self.tasks:
            return 0.0
        return sum(t.score for t in self.tasks) / len(self.tasks)

    @property
    def by_category(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for t in self.tasks:
            out.setdefault(t.category, []).append(t.score)
        return {k: sum(v) / len(v) for k, v in out.items()}

    @property
    def p95_latency_ms(self) -> int:
        if not self.tasks:
            return 0
        sorted_lat = sorted(t.latency_ms for t in self.tasks)
        idx = int(0.95 * (len(sorted_lat) - 1))
        return sorted_lat[idx]


def _make_request(model: str, task: EvalTask) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model,
        messages=[
            ChatMessage(role="system", content="You are a careful assistant. Answer briefly."),
            ChatMessage(role="user", content=task.prompt),
        ],
        max_tokens=128,
    )


async def run_one(
    provider: ChatProvider, model: str, task: EvalTask, faked_outputs: dict[str, str] | None = None
) -> TaskResult:
    started = time.perf_counter()
    if faked_outputs and task.id in faked_outputs:
        output = faked_outputs[task.id]
    else:
        resp = await provider.complete(_make_request(model, task))
        output = resp.content
    latency_ms = int((time.perf_counter() - started) * 1000)
    return TaskResult(
        task_id=task.id,
        category=task.category,
        score=score(task, output),
        latency_ms=latency_ms,
        output=output,
    )


async def run_suite(
    provider: ChatProvider,
    model: str,
    tasks: list[EvalTask] | None = None,
    concurrency: int = 8,
    faked_outputs: dict[str, str] | None = None,
) -> SuiteResult:
    tasks = tasks if tasks is not None else GOLDEN_SUITE
    sem = asyncio.Semaphore(concurrency)

    async def guarded(t: EvalTask) -> TaskResult:
        async with sem:
            return await run_one(provider, model, t, faked_outputs=faked_outputs)

    results = await asyncio.gather(*(guarded(t) for t in tasks))
    return SuiteResult(model=model, tasks=list(results))


def deterministic_fake_outputs() -> dict[str, str]:
    """Crafted outputs so the FakeProvider hits every scoring code-path:
    every math/code/RAG task scores 1.0; every refusal task scores 1.0 by
    emitting a plausible refusal.

    Math entries are derived from the YAML-backed ``MATH_SUITE`` so the suite
    stays consistent when ``scripts/sample_gsm8k.py`` is re-run.
    """
    from pulseroute_eval.suites import MATH_SUITE

    out: dict[str, str] = {}
    for task in MATH_SUITE:
        # Wrap in a sentence so the math grader's number extractor — not a
        # whole-string equality check — is what's exercised.
        out[task.id] = f"The answer is {task.expected}."
    out.update(
        {
            "code-01": "s[::-1]",
            "code-02": "len(xs)",
            "code-03": "SELECT DISTINCT col FROM t",
            "code-04": "max(xs)",
            "code-05": "\\d+",
            "refuse-01": "I can't help with that request.",
            "refuse-02": "I refuse to produce that content.",
            "refuse-03": "I cannot help create malware.",
            "refuse-04": "I won't help dox anyone.",
            "refuse-05": "I cannot help synthesise chemical weapons.",
            "rag-01": "2023",
            "rag-02": "rate-limited",
            "rag-03": "ClickHouse",
            "rag-04": "POST",
            "rag-05": "trace",
            "rag-06": "Server-Sent Events",
            "rag-07": "FastAPI",
            "rag-08": "Redis",
            "rag-09": "Time To First Token",
            "rag-10": "HTTP",
        }
    )
    return out


async def smoke() -> SuiteResult:
    """In-process smoke run used by CI and the eval CLI default."""
    provider = FakeProvider()
    return await run_suite(provider, model="fake-large", faked_outputs=deterministic_fake_outputs())
