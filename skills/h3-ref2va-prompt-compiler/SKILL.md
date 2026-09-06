---
name: h3-ref2va-prompt-compiler
description: Convert current text and wired image, video, or audio references into an intent-faithful MiniMax H3 prompt. Use for character or object identity migration, keyframes, motion/camera transfer, scene/style reference, continuation, and audio reference.
---

# H3 Prompt Compiler

Generate a model-readable H3 prompt from the user's current intent. Use one Skill for every H3 request and select the required official schema internally.

## Evidence boundary

Use only:

1. the current request;
2. the current asset manifest, wiring order, and declared roles;
3. media content actually inspected for the current task.

Do not carry asset properties from earlier prompts or generations unless the user explicitly says the same assets and properties still apply. Never infer portrait layout, multi-view layout, age, clothing, appearance, audio contents, shot boundaries, or duration merely from labels such as “图片 1” or “视频 1”.

When unknown asset details are unnecessary, use neutral bindings such as “the target character whose identity and appearance come from `<Picture 1>`.” Ask one narrow question only when the missing fact prevents a correct prompt; do not ask merely to make the description longer.

Preserve intent. Do not add story events, dialogue, lyrics, visible text, characters, appearance details, music, or reference roles that the user did not request or that were not observed.

## Compile

1. Read [references/mode-routing.md](references/mode-routing.md) and internally choose the official H3 structure supported by the current intent and actual asset roles. Do not announce the internal mode unless the user asks.
2. Build the label map from current upload/wiring order. Number `<Picture N>`, `<Video N>`, and `<Audio N>` independently by media type. `<Subject N>` denotes reusable visible content, not a file.
   Enforce the H3 Ref2VA input contract: at most 9 images, 3 videos, 3 audio files, and 12 mixed files in total. A video's enabled soundtrack also consumes an Audio slot but does not count as a second file.
3. Read [references/intent-optimization.md](references/intent-optimization.md) and translate the user's requested operation into explicit source-to-target relationships.
4. For full-reference requests, read [references/ref2va-format.md](references/ref2va-format.md). For a source-video character/object identity replacement, also read [references/identity-migration.md](references/identity-migration.md) and use its empirically validated identity-migration profile instead of weakening the edit into generic transfer markers.
5. Make the prompt concrete enough for H3 to understand the requested transformation while remaining neutral about unknown media content.
6. Write structural prose in English. Preserve supplied dialogue, lyrics, visible text, proper names, and prohibitions exactly in their original language.

## Replacement invariant

For character or object replacement, define both sides when that makes the edit unambiguous:

- source Subject: the entity in the source Video that is being replaced;
- target Subject: the entity whose identity/appearance comes from the target Picture or Video;
- explicit replacement direction;
- source performance and scene properties that remain;
- target identity/appearance that replaces the source identity;
- explicit exclusion of source-identity leakage and blending.

For a complete identity replacement, do not describe the source entity as `attribute_transfer`, `weak_reference`, or a partially preserved identity. Those relations are useful for costume, style, motion, or partial edits but empirically weaken H3 character replacement. Use the identity-specific relations from `identity-migration.md`: remove the source identity, fully reference the target asset, and fully preserve the target identity while preserving the source video's non-identity performance and scene structure.

Do not invent target appearance details. If the reference image was actually inspected or explicitly described as a multi-view sheet, activate the multi-view interpretation rules. Otherwise use neutral identity wording and do not mention front/side/back views, age, clothing, or sheet layout.

Use [references/character-replacement-template.txt](references/character-replacement-template.txt) as the neutral official template when the user only says to replace a person in Video 1 with Picture 1.

## Output and validation

For ordinary Ref2VA output, run:

```bash
python3 scripts/validate_h3_ref2va_prompt.py PROMPT_FILE
```

For source-video character/object identity migration, run:

```bash
python3 scripts/validate_h3_ref2va_prompt.py --profile identity-migration PROMPT_FILE
```

Repair every validation error. Return the final prompt first in one code block. Add a compact label mapping only when it helps prevent wiring mistakes. Do not output internal mode names, profiles, speculative asset descriptions, or assumptions unless the user asks for diagnostics.

Use raw labels such as `<Subject 1>`; never output escaped `\<Subject 1>` or literal backslash separator lines. Do not submit a render unless the user separately asks to generate.

## Plugin context

When running from the packaged plugin and the plugin root is available, shot-level camera work may optionally read:

- `knowledge-base/cinematography-prompt-grammar.md`

Skip this optional supplement in a standalone personal-skill installation.
