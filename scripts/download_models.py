#!/usr/bin/env python3
"""
Standalone script to download NLI model used by the commit classifier.

Downloads MoritzLaurer/deberta-v3-base-zeroshot-v2.0 (~440MB) from
HuggingFace Hub. Model is cached in ~/.cache/huggingface/hub/ and
reused by the InferenceEngine at runtime.

Usage:
    python scripts/download_models.py
    python scripts/download_models.py --verify
    python scripts/download_models.py --force

Same pattern as PolyEdge semantic_pipeline/download_models.py.
"""

import argparse
import sys
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# Default model — matches InferenceEngine default in commit_classifier.py
DEFAULT_MODEL = "MoritzLaurer/deberta-v3-base-zeroshot-v2.0"


def check_torch() -> bool:
    """Verify torch is installed and report device availability."""
    try:
        import torch
        log.info("torch %s installed", torch.__version__)

        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_mem / 1024**3
            log.info("  CUDA available: %s (%.1f GB VRAM)", name, vram)
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            log.info("  MPS available (Apple Silicon)")
        else:
            log.info("  CPU only (no GPU detected)")

        return True
    except ImportError:
        log.error("torch not installed. Install with:")
        log.error("  pip install torch>=2.0.0")
        log.error("  # For CUDA: pip install torch>=2.0.0 --index-url https://download.pytorch.org/whl/cu121")
        return False


def check_transformers() -> bool:
    """Verify transformers is installed."""
    try:
        import transformers
        log.info("transformers %s installed", transformers.__version__)
        return True
    except ImportError:
        log.error("transformers not installed. Install with:")
        log.error("  pip install transformers>=4.35.0")
        return False


def download_nli_model(model_name: str = DEFAULT_MODEL, force: bool = False) -> bool:
    """Download NLI model (tokenizer + weights).

    Uses HuggingFace from_pretrained() which caches to
    ~/.cache/huggingface/hub/ automatically. Subsequent calls
    are instant cache hits.
    """
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    log.info("Downloading NLI model: %s", model_name)
    log.info("This may take a few minutes on first download (~440MB)...")

    try:
        log.info("Downloading tokenizer...")
        tokenizer = AutoTokenizer.from_pretrained(
            model_name, force_download=force,
        )
        log.info("  Tokenizer downloaded")

        log.info("Downloading model weights (~440MB)...")
        model = AutoModelForSequenceClassification.from_pretrained(
            model_name, force_download=force,
        )
        log.info("  Model downloaded")
        log.info("  Parameters: %.0fM", sum(p.numel() for p in model.parameters()) / 1e6)
        log.info("  Labels: %s", model.config.id2label)

        return True
    except Exception as e:
        log.error("Failed to download %s: %s", model_name, e)
        return False


def verify_model(model_name: str = DEFAULT_MODEL) -> bool:
    """Run a test forward pass to verify model works end-to-end.

    Same warm-up pattern as InferenceEngine._warmup() — ensures
    the model can actually produce predictions, not just download.
    """
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification

    log.info("Verifying model with test forward pass...")

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)

        # Detect device
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        model.to(device)
        model.eval()
        log.info("  Model loaded on %s", device)

        # Test forward pass (same as InferenceEngine._warmup)
        premise = "fix login crash on submit button"
        hypothesis = "This change fixes a bug, corrects an error, or resolves broken behavior."

        inputs = tokenizer(
            premise, hypothesis,
            padding=True, truncation=True, max_length=256,
            return_tensors="pt",
        ).to(device)

        with torch.no_grad():
            outputs = model(**inputs)
            probs = torch.softmax(outputs.logits, dim=-1)

        id2label = model.config.id2label
        scores = {id2label[i]: probs[0][i].item() for i in range(len(probs[0]))}

        log.info("  Test classification scores:")
        for label, score in sorted(scores.items(), key=lambda x: -x[1]):
            log.info("    %-15s %.4f", label, score)

        # Sanity check: entailment should be highest for a clear bugfix premise
        entailment_key = [k for k in scores if "entail" in k.lower()]
        if entailment_key and scores[entailment_key[0]] > 0.5:
            log.info("  Entailment score > 0.5 for bugfix premise — model working correctly")
        else:
            log.warning("  Unexpected scores — model may not be performing as expected")

        # Memory report
        if device == "cuda":
            allocated = torch.cuda.max_memory_allocated() / 1024**2
            log.info("  Peak GPU memory: %.0f MB", allocated)
        elif device == "cpu":
            import os
            pid = os.getpid()
            try:
                import resource
                mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # macOS returns bytes, Linux returns KB
                if sys.platform == "darwin":
                    mem_mb = mem / 1024**2
                else:
                    mem_mb = mem / 1024
                log.info("  Peak RSS: %.0f MB", mem_mb)
            except Exception:
                pass

        log.info("  Model verification PASSED")
        return True

    except Exception as e:
        log.error("Model verification FAILED: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download NLI model for Reconcile commit classifier",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"HuggingFace model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run test forward pass after download to verify model works",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if cached",
    )

    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Reconcile NLI Model Setup")
    log.info("=" * 60)

    # Step 1: Check dependencies
    if not check_torch():
        sys.exit(1)
    if not check_transformers():
        sys.exit(1)

    # Step 2: Download model
    if not download_nli_model(args.model, force=args.force):
        sys.exit(1)
    print()

    # Step 3: Verify (optional but recommended)
    if args.verify:
        if not verify_model(args.model):
            sys.exit(1)
        print()

    log.info("=" * 60)
    log.info("Setup complete!")
    log.info("=" * 60)
    log.info("Model cached in: ~/.cache/huggingface/hub/")
    log.info("To use: the InferenceEngine loads this model automatically at startup.")
    if not args.verify:
        log.info("Run with --verify to test a forward pass.")


if __name__ == "__main__":
    main()
