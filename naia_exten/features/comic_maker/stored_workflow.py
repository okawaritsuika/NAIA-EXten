from __future__ import annotations

import json
import threading


class StoredWorkflow:
    """Load and render saved ComicPlans independently of NAI planning."""

    def _init_stored(self):
        self._stored_stop = threading.Event()
        self._stored_waiting = False
        self._stored_worker = None
        self._stored_pending = None
        self._stored_run = None
        self._stored_logs = []
        self._stored_recent_logs = []

    def _stored_busy(self):
        return bool(self._stored_waiting or self._stored_pending is not None or self._stored_run is not None
                    or (self._stored_worker and self._stored_worker.is_alive()))

    def _stored_status_fields(self):
        with self._run_lock:
            run, pending = self._stored_run, self._stored_pending
            waiting = self._stored_waiting
            logs = list(run.logs if run else self._stored_logs if waiting else self._stored_recent_logs)
            if waiting:
                summary = "저장 계획을 불러오는 중입니다..."
            elif run:
                total = run.pending.plan["page_count"]
                summary = f"저장 계획 이미지 생성 중 · 완료 {len(run.page_paths) + len(run.failures)}/{total}"
            elif pending:
                plan = pending.plan
                summary = f"저장 계획 · {plan['page_count']}페이지 · {plan['width']} × {plan['height']}"
            elif logs:
                summary = "저장 계획 최근 상태"
            else:
                return []
        fields = [{"key": "stored_summary", "type": "text", "label": "저장 계획",
                   "default": "", "placeholder": summary + ("\n" + "\n".join(logs) if logs else ""),
                   "multiline": True, "section": self.category}]
        if waiting or run:
            fields.append({"key": "reset_stored", "type": "action", "label": "저장 계획 작업 중지",
                           "help": "저장 계획 불러오기와 이미지 생성을 중지합니다."})
        elif pending:
            fields.extend([
                {"key": "confirm_stored", "type": "action", "label": "✓ 저장 계획으로 만들기"},
                {"key": "cancel_stored", "type": "action", "label": "저장 계획 취소"},
            ])
        return fields

    def _stop_stored(self):
        with self._run_lock:
            self._stored_stop.set()
            self._stored_waiting = False

    def _stored_fields(self):
        hidden = {"field": "__internal_never__", "in": ["1"]}
        with self._run_lock:
            state = {"nai_busy": self._nai_busy()}
        definitions = [
            {"key": "chooser_state", "type": "text", "placeholder": json.dumps(state)},
            {"key": "make_nai", "type": "action"},
            {"key": "make_ja", "type": "action"},
        ]
        return [{**item, "visible_when": hidden} for item in definitions]

    def _start_stored(self):
        with self._run_lock:
            if self._stored_busy():
                self._toast("이미 저장 계획 작업이 진행 중입니다.", "warning")
                return
            self._stored_waiting = True
            self._stored_logs.clear()
            self._stored_recent_logs.clear()
            self._stored_stop = threading.Event()
            self._stored_worker = threading.Thread(
                target=self._prepare,
                kwargs={"auto_generate": False, "source": "stored", "stop": self._stored_stop},
                daemon=True, name="comic-stored-plan",
            )
            self._stored_worker.start()
        self._refresh_panel()
