from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class PatchLayer:
    owner: str
    before: Optional[Callable] = None
    after: Optional[Callable] = None
    replace: Optional[Callable] = None


@dataclass
class PatchRecord:
    target: Any
    method_name: str
    original: Callable
    patched: Callable
    layers: list[PatchLayer] = field(default_factory=list)


@dataclass
class WebInjection:
    owner: str
    file_name: str
    marker: str
    content: str


class PatchManager:
    """Central composable runtime patch registry used by NAIA EXten features.

    Each host method receives one stable dispatcher. Independent feature layers
    are composed inside that dispatcher, so two features can safely observe or
    replace the same runtime path. Layers are removable by owner without
    rebuilding or disturbing the remaining owners.

    Front-end snippets use the same mechanism through one shared ``_web_file``
    layer. Snippets are collected separately so feature reloads cannot duplicate
    them or leave removed feature content in later responses.
    """

    WEB_PATCH_OWNER = "__naia_exten_web_injector__"

    def __init__(self):
        self._records: list[PatchRecord] = []
        self._web_injections: list[WebInjection] = []
        self._web_patch_installed = False

    def _find_record(self, target: Any, method_name: str) -> PatchRecord | None:
        return next(
            (
                record
                for record in self._records
                if record.target is target and record.method_name == method_name
            ),
            None,
        )

    def is_patched(self, target: Any, method_name: str) -> bool:
        return self._find_record(target, method_name) is not None

    def wrap_method(
        self,
        *,
        owner: str,
        target: Any,
        method_name: str,
        before: Optional[Callable] = None,
        after: Optional[Callable] = None,
        replace: Optional[Callable] = None,
    ) -> Callable:
        if not owner:
            raise ValueError("patch owner is required")
        if before is None and after is None and replace is None:
            raise ValueError("patch requires before, after, or replace")

        record = self._find_record(target, method_name)
        if record is None:
            original = getattr(target, method_name)
            if not callable(original):
                raise TypeError(f"{type(target).__name__}.{method_name} is not callable")

            record = PatchRecord(
                target=target,
                method_name=method_name,
                original=original,
                patched=lambda *args, **kwargs: None,
            )

            def patched(*args, **kwargs):
                # A generation already in flight must keep a stable chain even
                # if feature hot reload removes/rebuilds layers concurrently.
                layers = tuple(record.layers)
                return self._invoke_layers(
                    record.original,
                    layers,
                    len(layers) - 1,
                    args,
                    kwargs,
                )

            record.patched = patched
            setattr(target, method_name, patched)
            self._records.append(record)
        elif any(layer.owner == owner for layer in record.layers):
            raise RuntimeError(
                f"{type(target).__name__}.{method_name} is already patched by {owner}"
            )

        record.layers.append(
            PatchLayer(owner=owner, before=before, after=after, replace=replace)
        )
        return record.original

    @classmethod
    def _invoke_layers(
        cls,
        original: Callable,
        layers: tuple[PatchLayer, ...],
        index: int,
        args: tuple,
        kwargs: dict,
    ):
        if index < 0:
            return original(*args, **kwargs)

        layer = layers[index]
        if layer.before is not None:
            layer.before(*args, **kwargs)

        if layer.replace is not None:
            def next_layer(*next_args, **next_kwargs):
                return cls._invoke_layers(
                    original,
                    layers,
                    index - 1,
                    next_args,
                    next_kwargs,
                )

            result = layer.replace(next_layer, *args, **kwargs)
        else:
            result = cls._invoke_layers(original, layers, index - 1, args, kwargs)

        if layer.after is not None:
            maybe_result = layer.after(result, *args, **kwargs)
            if maybe_result is not None:
                result = maybe_result
        return result

    # ------------------------------------------------------------------
    # Shared front-end injection
    # ------------------------------------------------------------------

    def add_web_injection(
        self,
        *,
        owner: str,
        file_name: str,
        marker: str,
        content: str,
    ) -> None:
        file_name = str(file_name or "").strip()
        marker = str(marker or "").strip()
        if not owner or not file_name or not marker or not content:
            raise ValueError(
                "web injection requires owner, file_name, marker and content"
            )

        self._web_injections = [
            item
            for item in self._web_injections
            if not (
                item.owner == owner
                and item.file_name == file_name
                and item.marker == marker
            )
        ]
        self._web_injections.append(
            WebInjection(
                owner=owner,
                file_name=file_name,
                marker=marker,
                content=content,
            )
        )
        self._ensure_web_patch()

    def _ensure_web_patch(self) -> None:
        if self._web_patch_installed:
            return
        import app.backend.server.web_shell_routes as web_shell_routes

        self.wrap_method(
            owner=self.WEB_PATCH_OWNER,
            target=web_shell_routes,
            method_name="_web_file",
            replace=self._serve_web_file_with_injections,
        )
        self._web_patch_installed = True

    def _serve_web_file_with_injections(
        self,
        original,
        path,
        media_type,
        *args,
        **kwargs,
    ):
        path_obj = Path(path)
        matching = [
            item for item in self._web_injections if item.file_name == path_obj.name
        ]
        if not matching:
            return original(path, media_type, *args, **kwargs)

        try:
            source = path_obj.read_text(encoding="utf-8")
            changed = False
            for item in matching:
                if item.marker in source:
                    continue
                source += "\n\n" + item.content
                changed = True

            if not changed:
                return original(path, media_type, *args, **kwargs)

            from fastapi.responses import Response
            import app.backend.server.web_shell_routes as web_shell_routes

            return Response(
                content=source,
                media_type=media_type,
                headers=web_shell_routes._no_cache_headers(),
            )
        except Exception:
            return original(path, media_type, *args, **kwargs)

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore_owner(self, owner: str) -> None:
        self._web_injections = [
            item for item in self._web_injections if item.owner != owner
        ]

        for record in list(self._records):
            record.layers[:] = [
                layer for layer in record.layers if layer.owner != owner
            ]
            if record.layers:
                continue
            self._restore_record(record)

        if owner == self.WEB_PATCH_OWNER:
            self._web_patch_installed = False

    def _restore_record(self, record: PatchRecord) -> None:
        try:
            if getattr(record.target, record.method_name, None) is record.patched:
                setattr(record.target, record.method_name, record.original)
        finally:
            if record in self._records:
                self._records.remove(record)

    def restore_all(self) -> None:
        for record in list(reversed(self._records)):
            self._restore_record(record)
        self._web_injections.clear()
        self._web_patch_installed = False

    def list_patches(self) -> list[dict[str, str]]:
        items = [
            {
                "owner": layer.owner,
                "target": type(record.target).__name__,
                "method": record.method_name,
            }
            for record in self._records
            for layer in record.layers
        ]
        items.extend(
            {
                "owner": item.owner,
                "target": "web",
                "method": f"inject:{item.file_name}",
            }
            for item in self._web_injections
        )
        return items
