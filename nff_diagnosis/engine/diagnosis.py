from __future__ import annotations

import json
from typing import Any

import anthropic

from nff_diagnosis.engine.classifier import ClassificationResult, RuleClassifier
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput, ResolvedFrame

ENGINE_VERSION = "nff-diagnosis/0.1.0"

_SYSTEM_PROMPT = """\
You are an expert in embedded systems debugging, specialising in ESP32/Xtensa firmware crashes.

Your task: given a structured crash report, write ONE concise paragraph explaining why the device \
crashed. Cite specific function names and source locations when they are available in the resolved \
backtrace. Do not speculate beyond the evidence in the report. Do not add caveats or disclaimers — \
if the evidence is insufficient, say so plainly in one sentence. Output only the explanation \
paragraph, nothing else."""


class DiagnosisEngine:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
    ) -> None:
        self._classifier = RuleClassifier()
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def diagnose(
        self,
        crash: CrashRecord,
        resolved_backtrace: list[ResolvedFrame],
        location: dict[str, Any],
    ) -> DiagnosisOutput:
        classification = self._classifier.classify(crash)
        root_cause = self._call_claude(crash, resolved_backtrace, classification)
        return DiagnosisOutput(
            crash_id=crash.crash_id,
            crash_class=classification.crash_class,
            root_cause=root_cause,
            location=location,
            resolved_backtrace=resolved_backtrace,
            confidence=classification.confidence,
            candidates=classification.candidates,
            engine_version=ENGINE_VERSION,
        )

    def _call_claude(
        self,
        crash: CrashRecord,
        resolved_backtrace: list[ResolvedFrame],
        classification: ClassificationResult,
    ) -> str:
        top_frames = [f.model_dump() for f in resolved_backtrace[:5]]
        raw_excerpt = "\n".join(crash.raw.splitlines()[:60])

        user_content = json.dumps(
            {
                "crash_class": classification.crash_class,
                "confidence": classification.confidence,
                "classifier_rationale": classification.rationale,
                "panic_type": crash.panic_type,
                "exception_cause": crash.exception_cause,
                "fault_pc": crash.fault_pc,
                "fault_addr": crash.fault_addr,
                "task_name": crash.task_name,
                "top_backtrace_frames": top_frames,
                "raw_excerpt": raw_excerpt,
            },
            indent=2,
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
