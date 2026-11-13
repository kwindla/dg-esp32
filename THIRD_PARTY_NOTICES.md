# Third-party notices

## Little Voices derived character art

The angel and devil sprite packs in `assets/characters/angel/` and
`assets/characters/devil/`, with their shared `assets/characters/duo-build.cjs`
renderer, `little-voices.cjs`, `duo-raster.py` and proof helper adapt the stage renderer from
[Jon Taylor’s Little Voices](https://github.com/pipecat-ai/ios-little-voices),
revision `7f5613c` (Daily, 2026).

Source files: `FaceRig.swift`, `FaceGeometry.swift`, `FaceExpressions.swift`,
`FaceParameters.swift`, `RigMath.swift`, `DuoCharacter.swift`, `DuoStageView.swift`,
`StageRenderer.swift`, `StageLayout.swift`, `StageColors.swift`, `StagePalette.swift`,
`GlowSprites.swift`, `SeamRig.swift` and `LipsyncVisemes.swift`. The adaptation evaluates
settled source geometry, gradients, clipping and outlines as pre-rendered 412px
sprite patches. Energy drives speaking poses in place of phoneme lip-sync.

The upstream BSD 2-Clause license is reproduced below and in each pack’s `NOTICE`.

BSD 2-Clause License

Copyright (c) 2026, Daily

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
