"""
pp_doc_layout_builder.py  –  PP-DocLayout V3 layout builder for Marker
=======================================================================

Subclasses Marker's LayoutBuilder, overriding only `surya_layout()` to call
PP-DocLayout V3 instead of Surya.  All page structure wiring (add_block,
add_structure, polygon rescaling, expand_layout_blocks) is inherited from
the parent — no duplication.

Key design facts (confirmed from marker/builders/layout.py source):
  - LayoutBuilder.__call__ calls surya_layout() then add_blocks_to_pages()
  - add_blocks_to_pages does: page.add_block / page.add_structure / rescale
  - LayoutBox.label must be a valid BlockTypes enum member name
  - LayoutBox.polygon is [[x,y],...] in layout-image pixel space
  - LayoutResult.image_bbox = [0,0,W,H] tells add_blocks_to_pages the scale
  - add_blocks_to_pages rescales polygon from image pixels → PDF points automatically
"""

from __future__ import annotations

import logging
from typing import List, Optional

import torch
from PIL import Image
from transformers import AutoModelForObjectDetection

logger = logging.getLogger(__name__)

PP_MODEL_NAME        = "PaddlePaddle/PP-DocLayoutV3_safetensors"
CONFIDENCE_THRESHOLD = 0.3
BATCH_SIZE           = 4


# ══════════════════════════════════════════════════════════════════════════════
#  Label collapse: PP-DocLayout V3 class names → BlockTypes enum member names
#  (BlockTypes[label] must not KeyError inside add_blocks_to_pages)
# ══════════════════════════════════════════════════════════════════════════════

PP_TO_BLOCKTYPE: dict[str, str] = {
    # text / prose
    "paragraph":            "Text",
    "text":                 "Text",
    "plain text":           "Text",
    "reference":            "Text",
    "content":              "Text",
    "abstract":             "Text",
    # headings
    "title":                "SectionHeader",
    "section title":        "SectionHeader",
    "section_title":        "SectionHeader",
    "header":               "SectionHeader",
    "heading":              "SectionHeader",
    # tables
    "table":                "Table",
    "table_body":           "Table",
    "table_footnote":       "Caption",
    "table_caption":        "Caption",
    # figures / images
    "figure":               "Figure",
    "image":                "Figure",
    "figure_caption":       "Caption",
    "chart":                "Figure",
    # equations
    "equation":             "Equation",
    "formula":              "Equation",
    "math":                 "Equation",
    # lists
    "list":                 "ListItem",
    "list_item":            "ListItem",
    "bullet":               "ListItem",
    # page furniture
    "page_header":          "PageHeader",
    "page_footer":          "PageFooter",
    "footnote":             "Footnote",
    "caption":              "Caption",
    "doc_index":            "Text",
    "seal":                 "Picture",
    "watermark":            "Text",
    "background":           "Text",
    "stamp":                "Text",
    "toc":                  "Text",
    "signature":            "Text",
    "qr_code":              "Picture",
    "barcode":              "Picture",
    "doc_title":            "SectionHeader",
    "code":                 "Code",
    "algorithm":            "Text",
    "handwriting":          "Text",
}

_FALLBACK_BLOCKTYPE = "Text"


def _collapse_label(raw_label: str) -> str:
    normalized = raw_label.lower().strip()
    if normalized in PP_TO_BLOCKTYPE:
        return PP_TO_BLOCKTYPE[normalized]
    for key, value in PP_TO_BLOCKTYPE.items():
        if normalized.startswith(key):
            return value
    logger.debug("Unknown PP-DocLayout label %r → %r", raw_label, _FALLBACK_BLOCKTYPE)
    return _FALLBACK_BLOCKTYPE


# ══════════════════════════════════════════════════════════════════════════════
#  PP-DocLayout V3 inference wrapper
# ══════════════════════════════════════════════════════════════════════════════

class PPDocLayoutV3Model:
    """
    Inference wrapper for PP-DocLayout V3.

    Post-processing pattern confirmed from docling-pp-doc-layout reference:
      - AutoImageProcessor works via image_processor_type in preprocessor_config.json
      - post_process_object_detection is the correct call (not instance segmentation)
      - target_sizes must be [(H, W), ...] — PIL .size is (W, H), so reverse it
      - polygons come back as optional "polygons" or "polygon_points" keys on each result
      - fall back to box.tolist() XYXY if no polygon key present
    """

    def __init__(
        self,
        model_name: str = PP_MODEL_NAME,
        device: Optional[torch.device] = None,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.batch_size = batch_size
        self.device = device or (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        logger.info("Loading PP-DocLayout V3 from %s …", model_name)
        # No AutoImageProcessor — we implement preprocessing manually from
        # preprocessor_config.json: resize 800×800, divide by 255 (mean=0,std=1).
        # This avoids the AutoImageProcessor registry lookup that fails because
        # PPDocLayoutV3ImageProcessor is a custom class not in transformers.
        self.model = AutoModelForObjectDetection.from_pretrained(
            model_name, trust_remote_code=True
        )
        self.model.to(self.device)
        self.model.eval()
        logger.info("PP-DocLayout V3 loaded. id2label sample: %s",
                    list(self.model.config.id2label.values())[:8])

    def _preprocess(self, images: list[Image.Image]) -> dict[str, torch.Tensor]:
        """
        Manual preprocessing per preprocessor_config.json:
          do_resize:    True  → 800×800
          do_rescale:   True  → ×(1/255)
          do_normalize: True  → mean=[0,0,0], std=[1,1,1]  (no-op after rescale)
        """
        import torchvision.transforms.functional as TF
        tensors = []
        for img in images:
            t = TF.to_tensor(img.convert("RGB").resize((800, 800), Image.BILINEAR))
            tensors.append(t)  # [3,800,800], float32 in [0,1]
        return {"pixel_values": torch.stack(tensors).to(self.device)}

    def _post_process(
        self,
        outputs,
        original_sizes: list[tuple[int, int]],
    ) -> list[dict]:
        """
        DETR-style post-processing.
        pred_boxes: [B, Q, 4]  cx,cy,w,h normalised to [0,1]
        logits:     [B, Q, C+1]  last class is no-object
        original_sizes: [(H,W), ...] of the images BEFORE resize to 800×800.
        """
        import torch.nn.functional as F
        results = []
        probs = F.softmax(outputs.logits, dim=-1)
        scores, label_ids = probs[..., :-1].max(-1)
        for i, (s, l, boxes) in enumerate(zip(scores, label_ids, outputs.pred_boxes)):
            H, W = original_sizes[i]
            cx, cy, w, h = boxes.unbind(-1)
            x0 = (cx - 0.5 * w) * W
            y0 = (cy - 0.5 * h) * H
            x1 = (cx + 0.5 * w) * W
            y1 = (cy + 0.5 * h) * H
            pixel_boxes = torch.stack([x0, y0, x1, y1], dim=-1)
            mask = s > self.confidence_threshold
            results.append({
                "scores": s[mask].cpu(),
                "labels": l[mask].cpu(),
                "boxes":  pixel_boxes[mask].cpu(),
            })
        return results

    @torch.inference_mode()
    def predict_pages(self, images: list[Image.Image]) -> list[list[dict]]:
        """
        Returns one list per image, each entry:
          {"label": str, "confidence": float,
           "polygon": [[x0,y0],[x1,y0],[x1,y1],[x0,y1]] in input image pixel space}
        """
        all_results: list[list[dict]] = []

        for i in range(0, len(images), self.batch_size):
            batch = images[i : i + self.batch_size]
            # original sizes for coord de-normalisation; PIL .size = (W,H)
            original_sizes = [(img.height, img.width) for img in batch]
            inputs = self._preprocess(batch)
            outputs = self.model(**inputs)
            results = self._post_process(outputs, original_sizes)

            for result in results:
                detections: list[dict] = []
                for score, label_id, box in zip(
                    result["scores"], result["labels"], result["boxes"]
                ):
                    raw_label = self.model.config.id2label.get(label_id.item(), "text")
                    x0, y0, x1, y1 = box.tolist()
                    detections.append({
                        "label":      _collapse_label(raw_label),
                        "confidence": score.item(),
                        "polygon":    [[x0,y0],[x1,y0],[x1,y1],[x0,y1]],
                    })
                all_results.append(detections)

        return all_results


# ══════════════════════════════════════════════════════════════════════════════
#  LayoutBuilder subclass — only overrides surya_layout()
# ══════════════════════════════════════════════════════════════════════════════

class PPDocLayoutBuilder:
    """
    Drop-in replacement for Marker's LayoutBuilder.

    Subclasses LayoutBuilder and overrides only surya_layout() to call
    PP-DocLayout V3.  All page structure wiring (add_blocks_to_pages,
    expand_layout_blocks, page.structure, polygon rescaling) is inherited
    from LayoutBuilder — no duplication needed.

    __init__ accepts layout_model from Marker's dependency injector and
    discards it; PP-DocLayout V3 is loaded lazily on first call.
    """

    def __init__(self, layout_model=None, config=None) -> None:
        self.config = config or {}
        self._pp_model: Optional[PPDocLayoutV3Model] = None
        # Inherit parent attributes needed by add_blocks_to_pages /
        # expand_layout_blocks without calling LayoutBuilder.__init__
        # (which would try to use the Surya layout_model we don't want).
        self.force_layout_block = None
        self.disable_tqdm = False
        from marker.schema import BlockTypes
        self.expand_block_types = [
            BlockTypes.Picture,
            BlockTypes.Figure,
            BlockTypes.ComplexRegion,
        ]
        self.max_expand_frac = 0.05

    def _get_model(self) -> PPDocLayoutV3Model:
        if self._pp_model is None:
            self._pp_model = PPDocLayoutV3Model()
        return self._pp_model

    # ── Inherit these from LayoutBuilder without modification ─────────────
    # add_blocks_to_pages, expand_layout_blocks, get_batch_size, forced_layout

    def __call__(self, document, provider):
        """Mirror LayoutBuilder.__call__ exactly."""
        from marker.builders.layout import LayoutBuilder
        if self.force_layout_block is not None:
            layout_results = LayoutBuilder.forced_layout(self, document.pages)
        else:
            layout_results = self.surya_layout(document.pages)
        LayoutBuilder.add_blocks_to_pages(self, document.pages, layout_results)
        LayoutBuilder.expand_layout_blocks(self, document)

    def surya_layout(self, pages) -> list:
        """
        Run PP-DocLayout V3 on the pages and return List[LayoutResult]
        in the same format LayoutBuilder.surya_layout() returns.

        page.get_image(highres=False) is the same call the real LayoutBuilder
        makes — ensures coordinates are in the same pixel space.
        """
        from surya.layout.schema import LayoutResult, LayoutBox

        images = [p.get_image(highres=False) for p in pages]
        model = self._get_model()
        all_detections = model.predict_pages(images)

        results = []
        for img, detections in zip(images, all_detections):
            bboxes = []
            for position, det in enumerate(detections):
                label = det["label"]
                # Guard: skip any label that isn't a valid BlockTypes member
                try:
                    from marker.schema import BlockTypes
                    BlockTypes[label]
                except KeyError:
                    logger.warning("Skipping unknown BlockTypes label: %r", label)
                    continue
                bboxes.append(LayoutBox(
                    label=label,
                    position=position,
                    top_k={label: det["confidence"]},
                    polygon=det["polygon"],
                ))
            results.append(LayoutResult(
                image_bbox=[0, 0, img.width, img.height],
                bboxes=bboxes,
                sliced=False,
            ))
            logger.info("PP-DocLayout: %d boxes on page (image %dx%d)", len(bboxes), img.width, img.height)

        return results


# ══════════════════════════════════════════════════════════════════════════════
#  PdfConverter subclass factory
# ══════════════════════════════════════════════════════════════════════════════

def make_pp_converter(**kwargs):
    """
    Return a PdfConverter instance with PP-DocLayout V3 wired in.

    Overrides:
      self.layout_builder_class = PPDocLayoutBuilder  (replaces Surya)
      self.document              = captured Document   (for bbox overlay)
    """
    from marker.converters.pdf import PdfConverter

    class _PPDocPdfConverter(PdfConverter):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.layout_builder_class = PPDocLayoutBuilder
            self.document = None

        def build_document(self, filepath):
            doc = super().build_document(filepath)
            self.document = doc
            return doc

    return _PPDocPdfConverter(**kwargs)