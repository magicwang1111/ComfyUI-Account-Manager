import os
import shutil


def _path_parts(path: str):
    normalized = str(path or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None
    return parts


def iter_temp_references(value):
    if isinstance(value, dict):
        if value.get("type") == "temp" and value.get("filename"):
            yield value
        for nested in value.values():
            yield from iter_temp_references(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_temp_references(nested)


def persist_temp_assets(
    history_item: dict,
    temp_root: str,
    output_root: str,
    owner_slug: str,
    destination_subfolder: str,
    source_paths_by_id: dict[str, str] = None,
) -> list[tuple[dict, str]]:
    """Persist history-only temp references onto durable output paths."""
    owner_parts = _path_parts(owner_slug)
    destination_parts = _path_parts(destination_subfolder)
    if not owner_parts or len(owner_parts) != 1 or not destination_parts:
        return []

    owner_slug = owner_parts[0]
    destination_dir = os.path.join(output_root, *destination_parts)
    source_paths_by_id = source_paths_by_id or {}
    persisted_assets = []

    for reference in iter_temp_references(history_item):
        filename = str(reference.get("filename") or "")
        if not filename or filename != os.path.basename(filename):
            continue

        subfolder_parts = _path_parts(reference.get("subfolder"))
        if not subfolder_parts:
            continue

        destination = os.path.join(destination_dir, filename)
        if os.path.isfile(destination):
            reference["type"] = "output"
            reference["subfolder"] = "/".join(destination_parts)
            persisted_assets.append((reference, destination))
            continue

        owned_subfolder = (
            subfolder_parts[0] == owner_slug
            or (len(subfolder_parts) >= 2 and subfolder_parts[1] == owner_slug)
        )
        source_candidates = []
        reference_id = str(reference.get("id") or "")
        registered_source = source_paths_by_id.get(reference_id)
        if registered_source:
            source_candidates.append(registered_source)

        source_roots = [os.path.join(temp_root, owner_slug)]
        if owned_subfolder:
            source_roots.extend([os.path.join(temp_root, "public"), temp_root])
            if os.path.isdir(temp_root):
                source_roots.extend(
                    entry.path
                    for entry in os.scandir(temp_root)
                    if entry.is_dir(follow_symlinks=False)
                )

        for source_root in source_roots:
            candidate = os.path.join(source_root, *subfolder_parts, filename)
            if candidate not in source_candidates:
                source_candidates.append(candidate)

        source = next(
            (candidate for candidate in source_candidates if os.path.isfile(candidate)),
            None,
        )
        if source is None:
            continue

        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

        reference["type"] = "output"
        reference["subfolder"] = "/".join(destination_parts)
        persisted_assets.append((reference, destination))

    return persisted_assets
