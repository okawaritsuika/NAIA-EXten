from __future__ import annotations


class HostBridge:
    """
    Boundary for OPTIONAL/UNSTABLE access to NAIA internals.

    Prefer ExtensionContext public methods:
      subscribe, register_hook, register_panel, enqueue_generation,
      cancel_generation, get_current_request, get_api_mode,
      get_sampler_options, show_toast, get_result_image,
      get_save_directory, resolve_nai_characters,
      load_settings, save_settings, log

    v2.0.33 stores the host context privately as ctx._app_context, but that is
    NOT part of naia_ext_api=1. Any feature that needs it should access it only
    here so future NAIA updates require one compatibility fix.
    """

    def __init__(self, extension_ctx):
        self.ctx = extension_ctx

    @property
    def app_context(self):
        return getattr(self.ctx, "_app_context", None)

    @property
    def available(self) -> bool:
        return self.app_context is not None

    def getattr_path(self, dotted_path: str, default=None):
        current = self.app_context
        if current is None:
            return default

        for part in str(dotted_path).split("."):
            if not part:
                continue
            current = getattr(current, part, None)
            if current is None:
                return default
        return current
