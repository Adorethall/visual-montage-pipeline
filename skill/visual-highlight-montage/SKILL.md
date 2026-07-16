---
name: visual-highlight-montage
description: Run the local visual-montage-pipeline to create category-aware, music-driven multi-video ads with product openpage and recording assets, one continuous two-sentence product voiceover, branded endcard, and an editable JianYing cover title. Use for visual highlight compilations, beauty or food montage ads, batch creative production, resuming a run, or adding a configured visual category.
---

# Visual Highlight Montage

Use the project CLI as the execution engine. Do not recreate scoring, timing, packaging, cover, or JianYing logic in the agent context.

## Workflow

1. Resolve an absolute material manifest, Campaign YAML, BGM, and Asset Library.
2. Run input validation before paid or remote work.
3. Run or resume stages in order: `analyze`, `analyze-music`, `compose`, `package`, `cover`, `preview`, `render`, `jianying`, `validate`.
4. Read `data/runs/{run_id}/result.json` after every invocation.
5. Return the packaged MP4, clean/editable cover artifacts, JianYing result, and warnings.

## Hard requirements

- Keep product openpage immediately before product recording.
- Generate the two product sentences as one continuous voiceover file.
- Allow voiceover to extend no more than three seconds into the second montage and end before the endcard.
- Keep cover text as a native editable JianYing text segment; never make the baked preview the sole cover artifact.
- Treat Marlin as candidate recall only. Require Gemma/local validation before selection.
- Return `partial` when valid candidates are insufficient; never fill with weak or false events.

## Failures

- Retry a failed remote task according to the CLI policy, then resume from its stage.
- For weak Marlin recall, use broad queries, full-video Gemma discovery, then scene-frame discovery.
- Preserve the visual-core output when packaging voiceover or brand assets fail.
- Do not invent missing product assets, product capabilities, timestamps, or paths.

Read [references/input-output.md](references/input-output.md) when preparing a new invocation or diagnosing a result.

