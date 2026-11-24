#!/usr/bin/env python3
"""Generate the original, quiet household sound cues used by Vacation Ghost."""

import argparse
from array import array
from collections.abc import Callable, Iterator
import math
import os
from pathlib import Path
import random
import sys
import tempfile
import wave


SAMPLE_RATE = 16_000
PEAK = round(0.32 * 32_767)


def times(seconds: float) -> Iterator[float]:
    for sample in range(round(seconds * SAMPLE_RATE)):
        yield sample / SAMPLE_RATE


def tone(phase: float) -> float:
    return math.sin(math.tau * phase)


def noise(seed: int, smoothing: float) -> Iterator[float]:
    rng = random.Random(seed)
    previous = 0.0
    while True:
        previous += smoothing * (rng.uniform(-1, 1) - previous)
        yield previous


def kettle() -> Iterator[float]:
    steam = noise(1, 0.25)
    for time in times(5.0):
        pressure = min(time / 3, 1)
        whistle = max(0, pressure - 0.48) * tone(690 * time + 10 * time**2)
        release = min(1, max(0, (4.7 - time) * 2))
        hiss = next(steam) * (0.12 + pressure * 0.52)
        yield release * (hiss + 0.35 * whistle)


def dishwasher() -> Iterator[float]:
    water = noise(2, 0.08)
    for time in times(6.0):
        pump = 0.064 * tone(97 * time) + 0.038 * tone(194 * time)
        sweep = 0.37 + 0.15 * tone(0.67 * time) + 0.07 * tone(1.45 * time)
        yield min(1, time / 0.7, (6 - time) / 0.8) * (pump + sweep * next(water))


def footsteps() -> Iterator[float]:
    scuff = noise(3, 0.17)
    steps = ((0.53, 0.57), (1.17, 0.87), (1.72, 1.0), (2.39, 0.81), (3.15, 0.59))
    for time in times(4.0):
        texture = next(scuff)
        sample = 0.0
        for onset, weight in steps:
            age = time - onset
            if 0 <= age < 0.25:
                heel = tone(67 * age - 45 * age**2) * math.exp(-age * 33)
                rub = texture * math.exp(-age * 18)
                sample = weight * (0.8 * heel + 0.44 * rub)
                break
        yield sample


def cabinet() -> Iterator[float]:
    wood = noise(4, 0.16)
    for time in times(2.0):
        texture = next(wood)
        slide = max(0, 1 - abs(time - 0.67) / 0.43) * 0.4 * texture
        age = time - 1.13
        if age < 0:
            yield slide
            continue
        impact = math.exp(-age * 32) * (0.75 * tone(86 * age) + 0.20 * texture)
        latch_age = age - 0.044
        latch = 0.24 * tone(244 * latch_age) * math.exp(-latch_age * 110) if latch_age > 0 else 0
        yield impact + latch


def tv() -> Iterator[float]:
    room = noise(5, 0.04)
    syllables = (
        (0.65, 0.23, 124), (0.93, 0.17, 132), (1.15, 0.42, 118),
        (2.13, 0.20, 171), (2.37, 0.34, 177), (2.78, 0.27, 164),
        (3.11, 0.45, 157), (4.28, 0.34, 134), (4.68, 0.19, 127),
        (4.96, 0.53, 119), (6.54, 0.26, 175), (6.86, 0.25, 167),
        (7.15, 0.56, 157), (8.05, 0.24, 130), (8.35, 0.38, 123),
        (8.80, 0.50, 114), (9.68, 0.37, 172),
    )
    current = 0
    for time in times(10.6):
        voice = 0.0
        if current < len(syllables):
            onset, duration, pitch = syllables[current]
            age = time - onset
            if age >= duration:
                current += 1
            elif age > 0:
                envelope = min(1, age * 27, (duration - age) * 12)
                phase = pitch * age + 8 * age**2
                vowel = tone(phase) + 0.43 * tone(phase * 2) + 0.22 * tone(phase * 3)
                voice = 0.123 * envelope * vowel
        score = 0.035 * (tone(110 * time) + tone(165 * time) * tone(0.13 * time))
        yield voice + score + 0.08 * next(room)


def write_cue(destination: Path, make_samples: Callable[[], Iterator[float]]) -> bool:
    if destination.exists():
        return False
    samples = list(make_samples())
    scale = PEAK / max(abs(sample) for sample in samples)
    fade = round(0.035 * SAMPLE_RATE)
    pcm = array("h", (
        round(sample * scale * min(1, index / fade, (len(samples) - 1 - index) / fade))
        for index, sample in enumerate(samples)
    ))
    if sys.byteorder != "little":
        pcm.byteswap()

    # Write a complete file before publishing it, and preserve any custom cue at the destination.
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        staged_file = Path(staging) / destination.name
        with wave.open(str(staged_file), "wb") as output:
            output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            output.writeframes(pcm.tobytes())
        try:
            os.link(staged_file, destination)
        except FileExistsError:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="cue directory (existing files are preserved)")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name, make_samples in (
        ("kettle", kettle),
        ("dishwasher", dishwasher),
        ("footsteps", footsteps),
        ("cabinet", cabinet),
        ("tv", tv),
    ):
        destination = args.directory / f"{name}.wav"
        if write_cue(destination, make_samples):
            print(f"Created {destination}")
        else:
            print(f"Kept existing {destination}")


if __name__ == "__main__":
    main()
