"""
app.py  –  Marker + PP-DocLayout V3 + Qwen3.5 via HF Inference
==============================================================

HuggingFace Space entry point.

Pipeline per request
--------------------
1. layout_profiler.profile_layout()        CPU  – PyMuPDF font/geometry pass
2. Marker PdfConverter                      GPU  – layout + OCR + structure
   ├─ PP-DocLayout V3 (optional checkbox)   GPU  – replaces Surya layout
   └─ Qwen3.5 use_llm  (optional checkbox)  API  – HF inference router
3. Aside overlay                            CPU  – colour aside regions on
                                                   page-image using x-regime

Aside handling
--------------
Marker has no BlockTypes.Aside.  Rather than modifying Marker internals we:
  • Run layout_profiler to get the x-regime (body lane boundaries per page).
  • After conversion, render a bbox overlay image where regions whose
    centre-x falls outside the body lane are coloured orange (aside) instead
    of blue (body).
  • The markdown output is untouched — the visual overlay is the aside signal.

Credit guard
------------
The HF_TOKEN env var is set as a Space secret.  When the $2 free-tier credit
is exhausted the router returns HTTP 402.  We catch it once, flip a module-level
flag, and disable the LLM checkbox with an explanatory banner.
"""

from __future__ import annotations


import subprocess
import sys

# The HF Space Dockerfile evaluates requirements.txt before mounting the repo
# directory. We must compile and install our local marker fork at runtime.
subprocess.run(
    [
        sys.executable, "-m", "pip", "install",
        "./marker",
        "--quiet",
    ],
    check=True,
)

import os
import io
import logging
import tempfile
from typing import Optional

import gradio as gr
import spaces
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# ── Credit state  ─────────────────────────────────────────────────────────────
_credits_exhausted: bool = False

# ── HF Space / model config  ──────────────────────────────────────────────────
HF_TOKEN:    str = os.environ.get("HF_TOKEN", "")
QWEN_MODEL:  str = "Qwen/Qwen3.5-122B-A10B:cheapest"
HF_BASE_URL: str = "https://router.huggingface.co/v1"

# Aside overlay colours (RGBA)
BODY_COLOUR  = (59, 130, 246, 160)   # blue
ASIDE_COLOUR = (249, 115, 22, 160)   # orange
LABEL_COLOUR = (255, 255, 255, 220)  # white text bg

PP_MODEL_NAME = "PaddlePaddle/PP-DocLayoutV3_safetensors"


# ══════════════════════════════════════════════════════════════════════════════
#  Startup pre-download  (runs on CPU at Space launch, before any GPU request)
# ══════════════════════════════════════════════════════════════════════════════
#
# ZeroGPU only gates GPU compute — disk I/O runs freely at module load time.
# Pre-downloading here means the @spaces.GPU function never waits for network.

def _predownload_models() -> None:
    """
    ZeroGPU only gates GPU compute — disk I/O and network run freely at module
    load time.  By downloading here, _run_marker never waits for network I/O
    inside a paid GPU slot.

    Errors are logged but never re-raised — a failed pre-download means the
    first GPU request will download on-demand instead (slower, but not broken).
    """
    global _marker_models

    # ── Marker models (~2–3 GB) ────────────────────────────────────────────
    try:
        logger.info("Pre-downloading Marker models")
        from marker.models import create_model_dict
        _marker_models = create_model_dict()
        logger.info("Marker models ready.")
    except Exception as exc:
        logger.warning("Marker model pre-download failed: %s", exc)
        _marker_models = None

    # ── PP-DocLayout V3 (~400 MB) ──────────────────────────────────────────
    try:
        logger.info("Pre-downloading PP-DocLayout V3")
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=PP_MODEL_NAME,
            repo_type="model",
            ignore_patterns=["*.msgpack", "flax_model*", "tf_model*"],
        )
        logger.info("PP-DocLayout V3 ready.")
    except Exception as exc:
        logger.warning("PP-DocLayout V3 pre-download failed: %s", exc)


_marker_models = None   # populated by _predownload_models() at startup
_predownload_models()


# ══════════════════════════════════════════════════════════════════════════════
#  Overlay helpers
# ══════════════════════════════════════════════════════════════════════════════


# Dash pattern for x-regime lines (on, off) in pixels
_DASH = (6, 4)

def _draw_dashed_vline(draw, x: int, height: int, colour, dash=_DASH):
    """Draw a vertical dashed line at pixel x."""
    on, off = dash
    y = 0
    while y < height:
        draw.line([(x, y), (x, min(y + on, height))], fill=colour, width=2)
        y += on + off


def _draw_bbox_overlay(
    page_image: Image.Image,
    page_boxes: list[dict],   # [{"bbox": [x0,y0,x1,y1], "label": str}] in PDF points
    body_x_span,              # layout_profiler.XSpan | None  (also in PDF points)
) -> Image.Image:
    """
    Render coloured bbox overlay + x-regime guide lines on a page image at 72 dpi.

    At 72 dpi, 1 PDF point == 1 pixel — Marker block bboxes and layout_profiler
    x-span values are all in PDF points, so no coordinate conversion is needed.

    Visual language:
      Blue fill + outline   →  body block (centre-x inside body lane)
      Orange fill + outline →  aside block (centre-x outside body lane)
      Green dashed vlines   →  body lane left/right edges from layout_profiler
    """
    H = page_image.height
    overlay = page_image.convert("RGBA").copy()
    draw = ImageDraw.Draw(overlay, "RGBA")

    # ── Draw blocks ──────────────────────────────────────────────────────────
    for box in page_boxes:
        x0, y0, x1, y1 = box["bbox"]
        px0, py0, px1, py1 = int(x0), int(y0), int(x1), int(y1)
        cx = (x0 + x1) / 2.0

        is_aside = (
            body_x_span is not None
            and (cx < body_x_span.x_min or cx > body_x_span.x_max)
        )
        colour = ASIDE_COLOUR if is_aside else BODY_COLOUR
        draw.rectangle([px0, py0, px1, py1], outline=colour[:3] + (255,), width=2)
        draw.rectangle([px0, py0, px1, py1], fill=colour)

        label = box.get("label", "")
        if label:
            tw = len(label) * 7 + 4
            draw.rectangle([px0, py0, px0 + tw, py0 + 14], fill=LABEL_COLOUR)
            draw.text((px0 + 2, py0 + 1), label, fill=(30, 30, 30))

    # ── Draw x-regime guide lines ────────────────────────────────────────────
    if body_x_span is not None:
        XREGIME_COLOUR = (34, 197, 94, 230)   # green, semi-opaque
        _draw_dashed_vline(draw, int(body_x_span.x_min), H, XREGIME_COLOUR)
        _draw_dashed_vline(draw, int(body_x_span.x_max), H, XREGIME_COLOUR)

    return Image.alpha_composite(overlay, overlay).convert("RGB")


# ══════════════════════════════════════════════════════════════════════════════
#  Marker + PP-DocLayout GPU pipeline
# ══════════════════════════════════════════════════════════════════════════════

@spaces.GPU(duration=120)
def _run_marker(
    pdf_path: str,
    use_pp_layout: bool,
    use_llm: bool,
) -> tuple[str, list]:
    """
    GPU-decorated function: runs Marker conversion.
    Returns (markdown_text, rendered) where rendered is Marker's full output.

    Real API (confirmed from marker source / issues):
      - text_from_rendered returns (text, metadata, images) -- 3-tuple
      - LLM config keys: openai_api_key, openai_base_url, openai_model
      - llm_service must be passed as kwarg to PdfConverter
      - ConfigParser required to wire processors/renderer/llm_service
    """
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict   # fallback if predownload failed
    from marker.output import text_from_rendered

    config: dict = {"output_format": "markdown"}

    if use_llm:
        config.update({
            "use_llm":         True,
            "llm_service":     "marker.services.openai.OpenAIService",
            "openai_base_url": HF_BASE_URL,
            "openai_api_key":  HF_TOKEN,
            "openai_model":    QWEN_MODEL,
        })

    config_parser = ConfigParser(config)
    models = _marker_models if _marker_models is not None else create_model_dict()

    converter_kwargs = dict(
        config=config_parser.generate_config_dict(),
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )

    if use_pp_layout:
        # make_pp_converter returns a PdfConverter subclass instance that:
        #  - overrides self.layout_builder_class = PPDocLayoutBuilder
        #  - overrides build_document() to store the Document on self.document
        # This is the correct extension point per the actual pdf.py source.
        from pp_doc_layout_builder import make_pp_converter
        converter = make_pp_converter(**converter_kwargs)
    else:
        converter = PdfConverter(**converter_kwargs)

    rendered = converter(pdf_path)

    # text_from_rendered returns (text, metadata, images) -- must unpack
    text, _metadata, _images = text_from_rendered(rendered)

    # converter.document is populated by our build_document() override.
    # For vanilla PdfConverter (use_pp_layout=False) it will be None.
    document = getattr(converter, "document", None)
    n_pages = len(getattr(document, "pages", []))
    logger.info("_run_marker: document has %d pages (use_pp_layout=%s)", n_pages, use_pp_layout)
    return text, document


# ══════════════════════════════════════════════════════════════════════════════
#  Main processing function
# ══════════════════════════════════════════════════════════════════════════════

def process_pdf(
    pdf_file,
    use_pp_layout: bool,
    use_llm: bool,
) -> tuple[str, Optional[Image.Image], str]:
    """
    Full pipeline.  Returns:
      (markdown: str, overlay_image: PIL.Image | None, status: str)
    """
    global _credits_exhausted

    if pdf_file is None:
        return "", None, "⚠️ Please upload a PDF."

    if use_llm and _credits_exhausted:
        use_llm = False
        status_note = "⚠️ LLM disabled — HF inference credits exhausted.\n"
    else:
        status_note = ""

    if use_llm and not HF_TOKEN:
        return "", None, "⚠️ HF_TOKEN not set in Space secrets. Cannot use LLM."

    pdf_path = pdf_file.name if hasattr(pdf_file, "name") else str(pdf_file)

    # ── Stage 1: layout_profiler (CPU) ────────────────────────────────────
    try:
        from layout_profiler import profile_layout
        lp = profile_layout(pdf_path)
        profiler_summary = lp.summary()
    except Exception as exc:
        logger.warning("layout_profiler failed: %s", exc)
        lp = None
        profiler_summary = f"layout_profiler unavailable: {exc}"

    # ── Stage 2: Marker GPU pass ──────────────────────────────────────────
    try:
        markdown, document = _run_marker(pdf_path, use_pp_layout, use_llm)
    except Exception as exc:
        # Catch HF 402 credit exhaustion
        err_str = str(exc)
        if "402" in err_str:
            _credits_exhausted = True
            return "", None, "⚠️ HF inference credits exhausted. LLM disabled for this session."
        logger.exception("Marker conversion failed")
        return "", None, f"❌ Conversion error: {exc}"

    # ── Stage 3: Aside overlay (CPU) ──────────────────────────────────────
    # Render the page at 72 dpi.  PP-DocLayout detections are in the pixel
    # space of the images Marker passed to the builder (typically 96 dpi).
    # We scale them to 72 dpi using the ratio (overlay_w / det_img_w).
    overlay_image: Optional[Image.Image] = None
    try:
        import pymupdf as fitz
        doc = fitz.open(pdf_path)
        first_page = doc[0]
        mat = fitz.Matrix(1.0, 1.0)   # 72 dpi — 1 pt == 1 px
        pix = first_page.get_pixmap(matrix=mat)
        page_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()

        body_span = lp.body_regime.get_span(0) if lp is not None else None

        # Extract page_boxes from Marker's Document block tree.
        # document.pages[0].blocks holds the post-processed blocks.
        # block.polygon is [[x0,y0],[x1,y0],[x1,y1],[x0,y1]] in PDF points.
        # At 72 dpi, 1 PDF point == 1 pixel — no scaling needed.
        page_boxes: list[dict] = []
        if document is not None:
            pages = getattr(document, "pages", [])
            if pages:
                for block in getattr(pages[0], "children", []):
                    poly = getattr(block, "polygon", None)
                    if poly is None:
                        continue
                    xs = [p[0] for p in poly]
                    ys = [p[1] for p in poly]
                    bt = getattr(block, "block_type", None)
                    label = bt.value if hasattr(bt, "value") else str(bt)
                    page_boxes.append({
                        "bbox":  [min(xs), min(ys), max(xs), max(ys)],
                        "label": label,
                    })
            logger.info("Overlay: %d blocks from document.pages[0]", len(page_boxes))

        overlay_image = _draw_bbox_overlay(page_img, page_boxes, body_span)
    except Exception as exc:
        logger.warning("Overlay rendering failed: %s", exc)

    # ── Compose status message ─────────────────────────────────────────────
    mode_parts = []
    mode_parts.append("PP-DocLayout V3" if use_pp_layout else "Surya (default)")
    mode_parts.append("Qwen3.5 LLM" if use_llm else "no LLM")
    status = status_note + f"✅ Converted with {' + '.join(mode_parts)}\n\n{profiler_summary}"

    return markdown, overlay_image, status


# ══════════════════════════════════════════════════════════════════════════════
#  Gradio UI
# ══════════════════════════════════════════════════════════════════════════════

def _llm_checkbox_label() -> str:
    if _credits_exhausted:
        return "Use Qwen3.5 via HF Inference (⚠️ credits exhausted)"
    return "Use Qwen3.5 via HF Inference (uses $2 free credit)"


with gr.Blocks(title="Marker + PP-DocLayout V3") as demo:
    gr.Markdown(
        """
        # 📄 Marker PDF Converter
        ### with PP-DocLayout V3 & Qwen3.5

        Upload a PDF and choose your processing options.

        - **PP-DocLayout V3**: Replaces Surya layout detection with PaddlePaddle's
          RT-DETR instance segmentation model for higher bbox accuracy.
        - **Qwen3.5 LLM**: Enables Marker's `use_llm` pass for table cleanup,
          equation rendering, and reading-order correction via the HF inference router.

        The first page bbox overlay colours body regions **blue** and margin/aside
        regions **orange**, derived from font-geometric x-regime analysis.
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            pdf_input = gr.File(
                label="Upload PDF",
                file_types=[".pdf"],
                type="filepath",
            )
            with gr.Row():
                use_pp  = gr.Checkbox(label="Use PP-DocLayout V3",      value=True)
                use_llm = gr.Checkbox(label="Use Qwen3.5 (HF Inference)", value=False)

            run_btn = gr.Button("Convert", variant="primary")

        with gr.Column(scale=2):
            overlay_out = gr.Image(label="Page 1 — bbox overlay (blue=body, orange=aside)")

    with gr.Row():
        markdown_out = gr.Markdown(label="Converted Markdown")

    status_out = gr.Textbox(label="Status / Layout Profile", lines=12, interactive=False)

    run_btn.click(
        fn=process_pdf,
        inputs=[pdf_input, use_pp, use_llm],
        outputs=[markdown_out, overlay_out, status_out],
    )

    gr.Markdown(
        """
        ---
        **Notes**
        - LLM calls use your Space's `HF_TOKEN` free inference credit (~$2).
          Once exhausted, the LLM option is automatically disabled for the session.
        - Processing time: ~15-30s for a 10-page PDF without LLM; ~60s with LLM.
        - Overlay shows page 1 only. Aside detection uses PyMuPDF font-geometry
          (no extra GPU cost).
        """
    )


if __name__ == "__main__":
    demo.launch()