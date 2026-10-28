"""Placement policy shared by acceptance, recovery and monitoring.

Ollama 0.40.12 reports full offload only when size_vram == size, not when
rounded percentages approach 100. An unloaded model is an idle state; callers
performing a just-completed inference must instead require ``ok``.
"""


def evaluate_residency(models, model, policy="full", min_vram_fraction=0.5):
    if policy not in ("full", "partial"):
        raise ValueError("Unknown GPU offload policy")
    if (isinstance(min_vram_fraction, bool)
            or not isinstance(min_vram_fraction, (int, float))
            or not 0 < min_vram_fraction <= 1):
        raise ValueError("GPU fraction must be in (0, 1]")
    if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
        return "violation"
    selected = [item for item in models if item.get("name") == model]
    if not selected:
        return "idle"
    if len(selected) != 1:
        return "violation"
    size, vram = selected[0].get("size"), selected[0].get("size_vram")
    if type(size) is not int or type(vram) is not int or not 0 < vram <= size:
        return "violation"
    if policy == "full":
        return "ok" if vram == size else "violation"
    return "ok" if vram / size >= min_vram_fraction else "violation"
