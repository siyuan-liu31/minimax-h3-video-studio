# Image workflow profiles

MiniMax H3 Video Studio keeps image generation separate from MiniMax H3. Image profiles are
versioned, capability-probed ComfyUI graphs; unavailable models stay visible but
disabled rather than silently falling back to a different graph.

## Built-in quality profiles

| Profile | Task | Default sampling | Intended use |
| --- | --- | --- | --- |
| `z-image-turbo-bf16-t2i` | Text to image | 8 steps, CFG 1, `res_multistep/simple` | Default high-quality photorealism and Chinese/English text |
| `z-image-turbo-bf16-img2img` | One-image latent img2img, no LoRA | 8 steps, CFG 1, adjustable denoise | Default high-quality experimental redraw; not Z-Image Edit |
| `z-image-turbo-int8-t2i` | Text to image | 8 steps, CFG 1, `res_multistep/simple` | Low-memory/high-concurrency fallback |
| `z-image-turbo-int8-img2img` | One-image latent img2img, no LoRA | 8 steps, CFG 1, adjustable denoise | Low-memory/high-concurrency experimental redraw |
| `z-image-turbo-zit-nsfw-t2i` | Text to image + reviewed community LoRA | 8 steps, CFG 1, adjustable LoRA strength | Lawful consenting-adult generation; restricted community license |
| `z-image-turbo-zit-nsfw-img2img` | One-image latent img2img + reviewed community LoRA | 8 steps, CFG 1, adjustable LoRA strength and denoise | Experimental latent redraw; not official Z-Image Edit |
| `qwen-image-2512-fp8-t2i` | Text to image | 50 steps, CFG 4, `euler/simple` | Higher-quality people, natural detail and graphic layouts |
| `qwen-image-edit-2511-int8` | One-image instruction edit | 40 steps, CFG 3, `euler/simple`, denoise 1 | Identity-preserving edits, material/background/text changes |
| `qwen-image-2.1-bf16` | Text to image or 1–10 ordered image edits | 40 steps, CFG 1, `euler/simple` | Native 2K generation and instruction editing with full BF16 weights |
| `flux2-klein-4b-fp8` | Text or 1–4 ordered images | 4 steps, CFG 1, `euler/flux2` | Fast multi-reference editing; Apache-2.0 |
| `flux2-klein-9b-fp8` | Text or 1–4 ordered images | 4 steps, CFG 1, `euler/flux2` | Gated higher-capacity model; non-commercial only |
| `anything-v5-*` | Legacy text/image to image | 24 steps | Compatibility fallback only |

The Z-Image Turbo text-to-image graph follows the current official ComfyUI
template. Both Z-Image latent img2img profiles are reviewed experimental adapters:
they VAE-encode one source image and are not official Z-Image Edit workflows.
Z-Image Turbo uses a zeroed negative condition, so its UI intentionally hides
Negative Prompt. Qwen-Image 2512 receives the official language-aware quality
suffix in the server-compiled prompt, which remains visible in the final prompt
preview.

## Model files

```text
ComfyUI/models/
├── diffusion_models/
│   ├── z_image_turbo_bf16.safetensors
│   ├── z_image_turbo_int8_convrot.safetensors
│   ├── qwen_image_2512_fp8_e4m3fn.safetensors
│   ├── qwen_image_edit_2511_int8_convrot.safetensors
│   ├── qwen_image_2.1_bf16.safetensors
│   ├── flux-2-klein-4b-fp8.safetensors
│   └── flux-2-klein-9b-fp8.safetensors
├── text_encoders/
│   ├── qwen_3_4b_fp8_mixed.safetensors
│   ├── qwen_2.5_vl_7b_fp8_scaled.safetensors
│   ├── qwen_3_4b.safetensors
│   ├── qwen3vl_8b_bf16.safetensors
│   └── qwen_3_8b_fp8mixed.safetensors
└── vae/
    ├── ae.safetensors
    ├── qwen_image_vae.safetensors
    ├── qwen_image_2.1_vae_bf16.safetensors
    ├── flux2-vae.safetensors
    └── full_encoder_small_decoder.safetensors
└── loras/
    └── ZITnsfwLoRA.safetensors
```

Sources:

- Z-Image official repository: <https://github.com/Tongyi-MAI/Z-Image>
- Qwen-Image official repository: <https://github.com/QwenLM/Qwen-Image>
- Qwen-Image 2.1 official repository and research license: <https://github.com/QwenLM/Qwen-Image-2.1>
- Official ComfyUI Qwen-Image 2.1 guide: <https://docs.comfy.org/tutorials/image/qwen/qwen-image-2-1>
- Official ComfyUI templates: <https://github.com/Comfy-Org/workflow_templates>
- Official ComfyUI Z-Image guide: <https://docs.comfy.org/tutorials/image/z-image/z-image-turbo>
- Official ComfyUI Qwen Image Edit 2511 guide: <https://docs.comfy.org/tutorials/image/qwen/qwen-image-edit-2511>
- Official ComfyUI FLUX.2 Klein guide: <https://docs.comfy.org/tutorials/flux/flux-2-klein>
- FLUX.2 Klein 4B model card/license: <https://huggingface.co/black-forest-labs/FLUX.2-klein-4B>
- FLUX.2 Klein 9B model card/license: <https://huggingface.co/black-forest-labs/FLUX.2-klein-9B>
- ZIT NSFW LoRA v1 source and version metadata: <https://civitai.com/models/2279079?modelVersionId=2565112>

Z-Image and the earlier Qwen-Image profiles use their respective Apache-2.0 releases. **Qwen-Image 2.1 is different:** its weights use the Qwen Research License for non-commercial research and evaluation; commercial use needs a separate license. The pre-existing Anything V5 file has its
own upstream license and is not treated as the preferred quality profile.

## Qwen-Image 2.1 BF16 text generation and image editing

The `qwen-image-2.1-bf16` profile uses ComfyUI's native `TextEncodeQwenImage21`
and `QwenImage21Cache` nodes. A recent ComfyUI build exposing both nodes is
required. Capability probing also requires the exact three BF16 filenames above;
it will not substitute an FP8/INT8 model or silently reduce the requested size.
The ComfyUI cache uses `dtype=default`, and the model/encoder/vae loaders retain
their BF16 files. On a 32 GB GPU, ComfyUI may offload a resident component to
system RAM as needed; this is not weight quantization.

The reviewed [Comfy-Org/Qwen-Image-2.1 weights](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/tree/main)
have these SHA-256 values (verify downloads before enabling the profile):

| File | SHA-256 |
| --- | --- |
| `qwen_image_2.1_bf16.safetensors` | `89f4158d066cc33906a199fca85634f766892dd78f49b6698dabf187ac86c4bc` |
| `qwen3vl_8b_bf16.safetensors` | `68bdc82bc1b66851162ae656225e7e2068166b603db19bd5d5a3b90eb12669a9` |
| `qwen_image_2.1_vae_bf16.safetensors` | `bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9` |

With no image references, the same profile generates from text. With one to ten
images, it performs instruction-conditioned editing rather than a denoise-based
latent redraw. Reference images are bound in the submitted order. The first
image establishes the edited canvas; additional images are available for
identity, clothing, style or layout. Use explicit `<image1>`, `<image2>` etc. in
the prompt (Chinese `图1` / `图2` are canonicalized):

```bash
h3ctl generate image --profile qwen-image-2.1-bf16 \
  --prompt 'A blue ceramic teapot on a white table, product photograph' \
  --width 2048 --height 2048 --steps 40 --cfg 1 --seed 42 --wait

h3ctl generate image --profile qwen-image-2.1-bf16 \
  --ref ./subject.png --ref ./palette.png \
  --prompt 'Preserve the subject of <image1>; apply the color palette of <image2>.' \
  --width 2048 --height 2048 --steps 40 --cfg 1 --seed 43 --wait
```

The UI exposes this as one Image Generation profile, with an ordered reference
summary and native 2K aspect presets. The default is 40 steps / CFG 1. Seeds,
steps, CFG, negative prompt, and dimensions remain explicit; `denoise` is
intentionally not exposed because the native editing workflow does not use it.
The API only accepts dimensions on a 32-pixel grid within the reviewed 2K
pixel budget. The profile is not selected as an unavailable fallback.

## Original Z-Image Turbo latent img2img

`z-image-turbo-bf16-img2img` is the default original-Turbo latent profile. It
uses the official BF16 diffusion model and the complete `qwen_3_4b.safetensors`
encoder without loading the community LoRA. The INT8 profile remains available
only as a lower-memory/high-concurrency fallback. Both accept exactly one image
and run:

```text
LoadImage -> ImageScale -> VAEEncode -> KSampler(denoise < 1)
```

Sampling stays fixed at 8 steps, CFG 1, `res_multistep/simple`, shift 3. Start
with denoise 0.35–0.80; lower values preserve more source pixels while higher
values redraw more aggressively. This is latent redraw rather than semantic
instruction editing.

The model picker also exposes `Z-Image-Edit` as a disabled, non-selectable
roadmap entry. As of 2026-08-21 the official model zoo still labels both
Z-Image-Edit and Z-Image-Omni-Base **To be released**, with no reviewed
checkpoint binding or official ComfyUI graph. MiniMax H3 Video Studio will not substitute the
Turbo latent workflow and claim it is Z-Image-Edit.

FLUX.2 Klein 4B is Apache-2.0. FLUX.2 Klein 9B is gated and licensed only for
non-commercial use; accepting access on Hugging Face does not turn it into a
commercial license. Both remain subject to their model usage policy. MiniMax H3 Video Studio
does not advertise a safety-filter bypass.

## Z-Image Turbo + ZIT NSFW LoRA

The adult profiles are separate from the official Z-Image Turbo profile and
bind one exact reviewed community file:

```text
filename: ZITnsfwLoRA.safetensors
sha256: 44bf34ce695ebcec6ca17f7dc27511f8fc4204943114d6c7c41cd4559e75dbaf
```

Place the file in `ComfyUI/models/loras/`, verify the SHA-256, and refresh or
restart ComfyUI. MiniMax H3 Video Studio checks the exact filename through `/object_info`; it
does not silently substitute another LoRA. Both profiles keep Z-Image Turbo's
8-step, CFG 1, `res_multistep/simple`, shift 3 sampling graph. LoRA strength is
an independent model weight with a built-in range of 0–1.25.

The img2img profile accepts exactly one image. It encodes that image through
`LoadImage -> ImageScale -> VAEEncode` and sends the latent to KSampler. Denoise
controls redraw amount independently of LoRA strength; start around 0.35–0.80,
with 0.65 as the default. This is an experimental traditional latent img2img
adapter, not an official Z-Image Edit or reference-conditioning workflow.

The LoRA's Civitai permissions are restricted: local deployment is treated as
non-commercial, derivatives and redistribution are not enabled by this project,
and the upstream version page remains authoritative. Use is limited to lawful,
consensual adults. Minors, non-consensual intimate imagery, unauthorized sexual
deepfakes of real people, and illegal or infringing content are prohibited.

## FLUX.2 Klein multi-image references

The same Klein profile handles zero references (text-to-image) and one to four
ordered image references. Connect images to **Image Generation**, then reorder
them in the inspector. The canvas displays `图1 · Image 1` through `图4 · Image
4`; the server canonicalizes Chinese `图1` / `图片2`, English `picture 1` and
asset aliases to the official `image 1`, `image 2` syntax.

Use the first image as the main identity/composition anchor, then add narrower
references such as clothes, background and lighting/style. For example:

```text
Preserve the identity, facial features, pose and framing from image 1.
Apply the outfit from image 2, place the person in the background from image 3,
and use image 4 only for lighting and visual style.
```

Klein multi-reference is conditioning, not traditional latent img2img. There is
no denoise/reference-strength control and no Negative Prompt in this profile;
the distilled 4B and 9B profiles use fixed 4 steps and CFG 1. Reference order
and explicit natural-language relationships are the controls that matter.

## Extension contract

New image families must use a reviewed compiler identifier in
`server/profiles.py`; downloaded manifests may bind filenames and narrow trusted
parameter limits but cannot inject arbitrary ComfyUI classes or filesystem
paths. Add the compiler's non-removable node/model baseline, public defaults,
graph compiler, capability test, and remote `/object_info` validation together.
