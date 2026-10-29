"""Description file writer (LocalStatusItem.saveDescriptionData parity).

The first description line may carry the conversion option GUID
``{1F3AAA90-4782-4632-8C5E-E92F7A7751E4}=<option>`` (added by the parser).
``BINARY_PATCHES`` joins all lines with spaces and converts ``rowstart``
markers into line separators; otherwise each line is written as-is.
File name: the first component's file name with its extension stripped,
falling back to the object key. Target directory: the object sub-directory
when enabled, else the download store.
"""

from __future__ import annotations

from pathlib import Path

_DESC_CONV_GUID = "{1F3AAA90-4782-4632-8C5E-E92F7A7751E4}"
_LINE_SEP = "\n"


def component_base_name(components_file_names: list[str], dockey: str) -> str:
    """Hardening: basename only (no path traversal from portal data)."""
    if components_file_names:
        name = Path(components_file_names[0]).name
        name = name.replace("\\", "/").split("/")[-1]
        stem = Path(name).stem
        return stem or dockey
    return dockey


def save_description(
    store_dir: Path,
    dockey: str,
    description_lines: list[str],
    use_subdir: bool,
    first_component_file: str = "",
) -> Path:
    lines = list(description_lines)
    conversion = None
    if lines and lines[0].startswith(_DESC_CONV_GUID + "="):
        conversion = lines[0][len(_DESC_CONV_GUID) + 1 :].strip()
        lines = lines[1:]
    if not lines:
        raise ValueError("No description content to save.")

    file_name = component_base_name([first_component_file] if first_component_file else [], dockey)
    target_dir = Path(store_dir) / dockey if use_subdir else Path(store_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / file_name

    content: str
    if conversion == "BINARY_PATCHES":
        joined = " ".join(lines)
        content = joined.replace("rowstart", _LINE_SEP)
    else:
        content = _LINE_SEP.join(lines)

    target.write_text(content, encoding="utf-8")
    return target
