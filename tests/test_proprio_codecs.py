from __future__ import annotations

import json
from pathlib import Path

import torch

from dex_runtime.codecs import (
    ProprioCodec,
    ProprioCodecSpec,
    inhand_linker_g20_codec_spec,
    mounted_linker_g20_codec_spec,
)


FIXTURE = Path(__file__).parent / "fixtures/golden/proprio_codec_golden_v1.json"


def _run_trace(codec: ProprioCodec, samples: list[dict]) -> torch.Tensor:
    history = codec.empty_history()
    for sample in samples:
        frame = codec.encode_frame(sample["measured_rad"], sample["effective_target_rad"])
        history = codec.append(history, frame)
    return history


def test_runtime_mounted_codec_matches_cross_repository_golden_trace() -> None:
    fixture = json.loads(FIXTURE.read_text())
    spec = mounted_linker_g20_codec_spec(fixture["history_length"])
    codec = ProprioCodec(spec)
    history = _run_trace(codec, fixture["samples"])
    assert spec.control_period_ns == fixture["mounted"]["control_period_ns"]
    assert torch.allclose(history[-1], torch.tensor(fixture["mounted"]["expected_last_frame"]))
    assert codec.assemble_actor_input(history).shape == (32,)


def test_runtime_free_object_codec_matches_exact_96_value_golden_trace() -> None:
    fixture = json.loads(FIXTURE.read_text())
    spec = inhand_linker_g20_codec_spec(
        fixture["measured_lower_rad"],
        fixture["measured_upper_rad"],
        fixture["history_length"],
    )
    codec = ProprioCodec(spec)
    actor_input = codec.assemble_actor_input(_run_trace(codec, fixture["samples"]))
    assert spec.control_period_ns == fixture["free_object"]["control_period_ns"]
    assert actor_input.shape == (96,)
    assert torch.allclose(actor_input, torch.tensor(fixture["free_object"]["expected_actor_input"]))
    assert ProprioCodecSpec.from_dict(spec.as_dict()) == spec
